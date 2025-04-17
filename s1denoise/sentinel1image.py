from collections import defaultdict
from datetime import datetime, timedelta
import glob
import json
import os
from pathlib import Path
import requests
import shutil
import xml.etree.ElementTree as ET
import zipfile
from functools import lru_cache

import numpy as np
from osgeo import gdal
from bs4 import BeautifulSoup
from scipy.interpolate import InterpolatedUnivariateSpline, RectBivariateSpline
from scipy.optimize import minimize
from scipy.interpolate import interp1d
from scipy.ndimage import gaussian_filter

from s1denoise.utils import (
    cost,
    fit_noise_scaling_coeff,
    fill_gaps,
    cubic_hermite_interpolation,
    parse_azimuth_time,
)

SPEED_OF_LIGHT = 299792458.
RADAR_FREQUENCY = 5.405000454334350e+09
RADAR_WAVELENGTH = SPEED_OF_LIGHT / RADAR_FREQUENCY
ANTENNA_STEERING_RATE = { 'IW1': 1.590368784,
                          'IW2': 0.979863325,
                          'IW3': 1.397440818,
                          'EW1': 2.390895448,
                          'EW2': 2.811502724,
                          'EW3': 2.366195855,
                          'EW4': 2.512694636,
                          'EW5': 2.122855427 }    # degrees per second. Available from AUX_INS

class Sentinel1ImageXml:
    def __init__(self, s1):
        ''' Read calibration/annotation XML files and auxiliary XML file '''        
        self.txPol = s1.filename.split(os.sep)[-1][15]    # H or V
        self.platform = s1.filename.split(os.sep)[-1][:3]    # S1A or S1B
        manifest_file = [f for f in s1.filenames if 'manifest.safe' in f][0]

        # find annotation, calibration and noise files
        xml_names = ['annotation', 'calibration', 'noise']
        self.__dict__.update({name : {} for name in xml_names})
        for pol in [self.txPol + 'H', self.txPol + 'V']:
            self.annotation[pol] = [f for f in s1.filenames if ('annotation/s1' in f and pol.lower() in f)][0]
            self.calibration[pol] = [f for f in s1.filenames if ('calibration-s1' in f and pol.lower() in f)][0]
            self.noise[pol] = [f for f in s1.filenames if ('noise-s1' in f and pol.lower() in f)][0]

        # read and parse XML files
        if zipfile.is_zipfile(s1.filename):
            with zipfile.PyZipFile(s1.filename) as zf:
                for name in xml_names:
                    for pol in self.__dict__[name]:
                        self.__dict__[name][pol] = BeautifulSoup(zf.read(self.__dict__[name][pol]), features="xml")
                self.manifest = BeautifulSoup(zf.read(manifest_file), features="xml")
        else:
            for name in xml_names:
                for pol in self.__dict__[name]:
                    with open(self.__dict__[name][pol]) as ff:
                        self.__dict__[name][pol] = BeautifulSoup(ff.read(), features="xml")
            with open(manifest_file) as ff:
                self.manifest = BeautifulSoup(ff.read(), features="xml")

        # get the auxiliary calibration file
        for resource in (self.manifest.find_all('resource') +
                         self.manifest.find_all('safe:resource')):
            if resource.attrs['role'] == 'AUX_CAL':
                auxCalibFilename = resource.attrs['name'].split('/')[-1]
        self.set_aux_data_dir()
        self.download_aux_calibration(auxCalibFilename)
        with open(self.auxiliaryCalibration_file) as f:
            self.auxiliary = BeautifulSoup(f.read(), features="xml")

    def set_aux_data_dir(self):
        """ Set directory where aux calibration data is stored """
        self.aux_data_dir = os.path.join(os.environ.get('XDG_DATA_HOME', os.path.expanduser('~')),
                                         '.s1denoise')
        if not os.path.exists(self.aux_data_dir):
            os.makedirs(self.aux_data_dir)

    def download_aux_calibration(self, filename):
        """ Download AUX calibration file from APC """
        auxarchive = f"{self.platform}_AUX_CAL_20241128"
        auxarchive_path = Path(self.aux_data_dir) / auxarchive

        # Check if the AUX calibration file for current product already exists
        vs = filename.split('_')[3].lstrip('V')
        vs_year = vs[:4]
        vs_month = vs[4:6]
        vs_hour = vs[6:8]
        subdirs = f"{self.platform}/AUX_CAL/{vs_year}/{vs_month}/{vs_hour}"
        self.auxiliaryCalibration_file = auxarchive_path / subdirs / filename / "data" / f"{self.platform.lower()}-aux-cal.xml"
        if auxarchive_path.exists():
            # the unzipped archive already exists
            if self.auxiliaryCalibration_file.exists():
                # and contains the calibration file
                return
            else:
                # but does not include the calibration file
                raise FileNotFoundError(f"AUX calibration archive does not include {filename}. This is probably because your product was created with an IPF version after 11/2024.")
        
        # Download archive containing AUX files
        auxarchive_url = f"https://sar-mpc.eu/files/{auxarchive}.zip"
        print(f'Downloading AUX calibration archive from {auxarchive_url}')
        
        with requests.get(auxarchive_url, stream=True) as r:
            with open(auxarchive_path.with_suffix(".zip"), "wb") as f:
                f.write(r.content)

        with zipfile.ZipFile(auxarchive_path.with_suffix(".zip"), 'r') as download_zip:
            download_zip.extractall(path=auxarchive_path)
        
        # Check if archive contains file for current product
        if not self.auxiliaryCalibration_file.exists():
            raise FileNotFoundError(f"AUX calibration archive does not include {filename}. This is probably because your product was created with an IPF version after 11/2024.")

class Sentinel1Image():
    """ Thermal noise correction for S1 GRD data

    Input
    -----
    filename : str
        filename of the Sentinel-1 GRD file (SAFE or ZIP format)

    Returns
    -------
    s1 : Sentinel1Image
        Object to perform noise correction
    """
    def __init__(self, filename):
        self.filename = filename
        self.find_filesnames()
        # get list of measurements
        self.measurements = {os.path.basename(f).split('-')[3].upper() : f for f in self.filenames if 'measurement/s1' in f}
        if self.is_zipfile:
            for pol in self.measurements:
                self.measurements[pol] = f'/vsizip/{filename}/{self.measurements[pol]}'

        if (os.path.basename(self.filename)[4:16] not in [
            'IW_GRDH_1SDH', 'IW_GRDH_1SDV', 'EW_GRDM_1SDH', 'EW_GRDM_1SDV']):
            raise ValueError(
                'Source file must be Sentinel-1A/1B '
                'IW_GRDH_1SDH, IW_GRDH_1SDV, EW_GRDM_1SDH, or EW_GRDM_1SDV product.')
        self.obsMode = self.filename.split(os.sep)[-1][4:6]    # IW or EW
        pol_mode = os.path.basename(self.filename).split('_')[3]
        self.crosspol = {'1SDH': 'HV', '1SDV': 'VH'}[pol_mode]
        self.pols = {'1SDH': ['HH', 'HV'], '1SDV': ['VH', 'VV']}[pol_mode]
        self.swath_ids = range(1, {'IW':3, 'EW':5}[self.obsMode]+1)
        # scene center time will be used as the reference for relative azimuth time in seconds
        self.time_coverage_center = ( self.time_coverage_start + timedelta(
            seconds=(self.time_coverage_end - self.time_coverage_start).total_seconds()/2) )
        self.xml = Sentinel1ImageXml(self)
        # get processor version of Sentinel-1 IPF (Instrument Processing Facility)
        self.IPFversion = float(self.xml.manifest.find('safe:software').attrs['version'])
        if self.IPFversion < 2.43:
            print('\nERROR: IPF version of input image is lower than 2.43! '
                  'Denoising vectors in annotation files are not qualified. '
                  'Only TG-based denoising can be performed\n')
        elif 2.43 <= self.IPFversion < 2.53:
            print('\nWARNING: IPF version of input image is lower than 2.53! '
                  'ESA default noise correction result might be wrong.\n')

    def find_filesnames(self):
        """ Find all filenames in subdirecotries """
        self.is_zipfile = False
        if zipfile.is_zipfile(self.filename):
            self.is_zipfile = True
            with zipfile.PyZipFile(self.filename) as zf:
                self.filenames = zf.namelist()
        else:
            self.filenames = [str(i) for i in Path(f'{self.filename}/').rglob('*')]

    @property
    def time_coverage_start(self):
        return datetime.strptime(os.path.basename(self.filename).split('_')[4], '%Y%m%dT%H%M%S')

    @property
    def time_coverage_end(self):
        return datetime.strptime(os.path.basename(self.filename).split('_')[5], '%Y%m%dT%H%M%S')

    @lru_cache(maxsize=None)
    def shape(self, pol):
        """ Shape of the raster for input polarization """
        a = self.xml.annotation[pol]
        return [int(a.find(i).text) for i in ['numberOfLines', 'numberOfSamples']]

    @lru_cache(maxsize=None)
    def swath_bounds(self, pol):
        """ Boundaries of blocks in each swath for each polarisation """
        names = {
            'azimuthTime' : parse_azimuth_time,
            'firstAzimuthLine' : int,
            'firstRangeSample' : int,
            'lastAzimuthLine' : int,
            'lastRangeSample' : int,
        }
        swath_bounds = {}
        for swathMerge in self.xml.annotation[pol].find_all('swathMerge'):
            swath_bounds[swathMerge.swath.text] = defaultdict(list)
            for swathBounds in swathMerge.find_all('swathBounds'):
                for name in names:
                    swath_bounds[swathMerge.swath.text][name].append(names[name](swathBounds.find(name).text))
        return swath_bounds

    @lru_cache(maxsize=None)
    def geolocation(self, pol):
        ''' Import geolocationGridPoint from annotation XML '''
        geolocation_keys = {
            'azimuthTime':   str, #lambda x : datetime.strptime(x, '%Y-%m-%dT%H:%M:%S.%f'),
            'slantRangeTime':float,
            'line':  int,
            'pixel': int,
            'latitude':float,
            'longitude':float,
            'height':float,
            'incidenceAngle':float,
            'elevationAngle':float,
        }
        geolocation = defaultdict(list)
        for p in self.xml.annotation[pol].find_all('geolocationGridPoint'):
            for c in p:
                if c.name:
                    geolocation[c.name].append(geolocation_keys[c.name](c.text))
        geolocation['line'] = np.unique(geolocation['line'])
        geolocation['pixel'] = np.unique(geolocation['pixel'])
        for i in geolocation:
            if i not in ['line', 'pixel']:
                geolocation[i] = np.array(geolocation[i]).reshape(
                    geolocation['line'].size,
                    geolocation['pixel'].size
                    )
        return geolocation

    @lru_cache(maxsize=None)
    def geolocation_relative_azimuth_time(self, pol):
        """ Matrix with relative azimuth time (AT) computed as second difference of AT from central AT """
        az_time = list(map(parse_azimuth_time, self.geolocation(pol)['azimuthTime'].flat))
        az_time = [ (t-self.time_coverage_center).total_seconds() for t in az_time]
        az_time = np.array(az_time).reshape(self.geolocation(pol)['azimuthTime'].shape)
        return az_time

    @lru_cache(maxsize=None)
    def calibration(self, pol):
        """ Import calibrationVector from calibration XML """
        calibration = defaultdict(list)
        for cv in self.xml.calibration[pol].find_all('calibrationVector'):
            for n in cv:
                if n.name:
                    calibration[n.name].append(n.text)
        calibration['azimuthTime'] = list(map(parse_azimuth_time, calibration['azimuthTime']))
        calibration['line'] = np.array(list(map(int, calibration['line'])))
        calibration['pixel'] = np.array([list(map(int, p.split())) for p in calibration['pixel']])
        for key in ['sigmaNought', 'betaNought', 'gamma', 'dn']:
            calibration[key] = np.array([list(map(float, p.split())) for p in calibration[key]])
        return calibration

    @lru_cache(maxsize=None)
    def aux_calibration_params(self):
        """ Import calibrationParams from AUX calibration XML """
        swaths = [f'{self.obsMode}{li}' for li in self.swath_ids]
        calibration_params = {pol : {swath: {} for swath in swaths} for pol in self.pols}
        for calibrationParams in self.xml.auxiliary.find_all('calibrationParams'):
            swath = calibrationParams.swath.text
            pol = calibrationParams.polarisation.text
            if pol in calibration_params and swath in calibration_params[pol]:
                calibration_params[pol][swath]['elevationAngleIncrement'] = float(calibrationParams.elevationAntennaPattern.elevationAngleIncrement.text)
                calibration_params[pol][swath]['azimuthAngleIncrement'] = float(calibrationParams.azimuthAntennaElementPattern.azimuthAngleIncrement.text)
                calibration_params[pol][swath]['absoluteCalibrationConstant'] = float(calibrationParams.absoluteCalibrationConstant.text)
                calibration_params[pol][swath]['noiseCalibrationFactor'] = float(calibrationParams.noiseCalibrationFactor.text)
                calibration_params[pol][swath]['elevationAntennaPatternCount'] = int(calibrationParams.elevationAntennaPattern.values.attrs['count'])
                calibration_params[pol][swath]['elevationAntennaPattern'] = np.array([float(i) for i in calibrationParams.elevationAntennaPattern.values.text.split()])
                calibration_params[pol][swath]['azimuthAntennaPattern'] = np.array([float(i) for i in calibrationParams.azimuthAntennaElementPattern.values.text.split()])
        return calibration_params

    @lru_cache(maxsize=None)
    def noise_range(self, pol):
        """ Read range noise vectors from noise XML """
        if self.IPFversion < 2.9:
            noiseRangeVectorName = 'noiseVector'
            noiseLutName = 'noiseLut'
        else:
            noiseRangeVectorName = 'noiseRangeVector'
            noiseLutName = 'noiseRangeLut'
        noise_range = defaultdict(list)
        for noiseVector in self.xml.noise[pol].find_all(noiseRangeVectorName):
            noise_range['azimuthTime'].append(parse_azimuth_time(noiseVector.azimuthTime.text))
            noise_range['line'].append(int(noiseVector.line.text))
            noise_range['pixel'].append(np.array([int(i) for i in noiseVector.pixel.text.split()]))
            noise_range['noise'].append(np.array([float(i) for i in noiseVector.find(noiseLutName).text.split()]))
        noise_range['line'] = np.array(noise_range['line'])
        return noise_range

    @lru_cache(maxsize=None)
    def noise_azimuth(self, pol):
        """ Read azimuth noise vectors from noise XML """
        noise_azimuth = {f'{self.obsMode}{swid}':defaultdict(list) for swid in self.swath_ids}
        if self.IPFversion < 2.9:
            for swid in self.swath_ids:
                noise_azimuth[f'{self.obsMode}{swid}'] = dict(
                    firstAzimuthLine = [0],
                    firstRangeSample = [0],
                    lastAzimuthLine = [self.shape(pol)[0]-1],
                    lastRangeSample = [self.shape(pol)[1]-1],
                    line = [np.array([0, self.shape(pol)[0]-1])],
                    noise = [np.array([1.0, 1.0])])
        else:
            int_names = ['firstAzimuthLine', 'firstRangeSample', 'lastAzimuthLine', 'lastRangeSample']
            for noiseAzimuthVector in self.xml.noise[pol].find_all('noiseAzimuthVector'):
                swath = noiseAzimuthVector.swath.text
                for int_name in int_names:
                    noise_azimuth[swath][int_name].append(int(noiseAzimuthVector.find(int_name).text))
                noise_azimuth[swath]['line'].append(np.array([int(i) for i in noiseAzimuthVector.line.text.split()]))
                noise_azimuth[swath]['noise'].append(np.array([float(i) for i in noiseAzimuthVector.noiseAzimuthLut.text.split()]))
        return noise_azimuth

    def get_swath_id_vectors(self, pol, pixel=None):
        """ Create vectors with IDs of swaths for each range noise vector """
        if pixel is None:
            pixel = self.noise_range(pol)['pixel']
        swath_indices = [np.zeros(p.shape, int) for p in pixel]
        swathBounds = self.swath_bounds(pol)

        for iswath, swath_name in enumerate(swathBounds):
            swathBound = swathBounds[swath_name]
            zipped = zip(
                swathBound['firstAzimuthLine'],
                swathBound['lastAzimuthLine'],
                swathBound['firstRangeSample'],
                swathBound['lastRangeSample'],
            )
            for fal, lal, frs, lrs in zipped:
                valid1 = np.where(
                    (self.noise_range(pol)['line'] >= fal) *
                    (self.noise_range(pol)['line'] <= lal)
                    )[0]
                for v1 in valid1:
                    valid2 = (pixel[v1] >= frs) * (pixel[v1] <= lrs)
                    swath_indices[v1][valid2] = iswath + 1
        return swath_indices

    def get_eap_rsl_vectors(self, pol, min_size=3, rsl_power=3./2.):
        """ Compute Antenna Pattern Gain APG=(1/EAP/RSL)**2 for each noise vectors """
        pixel = self.noise_range(pol)['pixel']
        eap_vectors = [np.zeros(p.size) + np.nan for p in pixel]
        rsl_vectors = [np.zeros(p.size) + np.nan for p in pixel]
        swath_indices = self.get_swath_id_vectors(pol)

        for swid in self.swath_ids:
            swath_name = f'{self.obsMode}{swid}'
            eap_interpolator = self.get_eap_interpolator(swath_name, pol)
            ba_interpolator = self.get_boresight_angle_interpolator(pol)
            rsl_interpolator = self.get_range_spread_loss_interpolator(pol, rsl_power=rsl_power)
            for i, (l, p, swids) in enumerate(zip(self.noise_range(pol)['line'], pixel, swath_indices)):
                gpi = np.where(swids == swid)[0]
                if gpi.size > min_size:
                    ba = ba_interpolator(l, p[gpi]).flatten()                    
                    eap = eap_interpolator(ba).flatten()
                    rsl = rsl_interpolator(l, p[gpi]).flatten()
                    eap_vectors[i][gpi] = eap
                    rsl_vectors[i][gpi] = rsl

        return eap_vectors, rsl_vectors

    def get_pg_product(self, pol, pg_name='pgProductAmplitude'):
        """ Read PG-product from annotation XML """
        azimuth_time = [(t - self.time_coverage_center).total_seconds()
                        for t in self.noise_range(pol)['azimuthTime']]
        pg = defaultdict(dict)
        for pgpa in self.xml.annotation[pol].find_all(pg_name):
            pg[pgpa.parent.parent.parent.swath.text][pgpa.parent.azimuthTime.text] = float(pgpa.text)

        pg_swaths = {}
        for swid in pg:
            rel_az_time = np.array([
            (datetime.strptime(t, '%Y-%m-%dT%H:%M:%S.%f') - self.time_coverage_center).total_seconds()
            for t in pg[swid]])
            pgvec = np.array([pg[swid][i] for i in pg[swid]])
            sortIndex = np.argsort(rel_az_time)
            pg_interp = InterpolatedUnivariateSpline(rel_az_time[sortIndex], pgvec[sortIndex], k=1)
            pg_vec = pg_interp(azimuth_time)
            pg_swaths[swid] = pg_vec
        return pg_swaths

    def get_tg_vectors(self, pol, min_size=3):
        """ Compute Total Gain TG=Gtot*/(EAP/RSL)**2 for each noise vectors """
        eap_vectors, rsl_vectors = self.get_eap_rsl_vectors(pol, min_size=min_size, rsl_power=2)
        tg_vectors = [(1 / eap / rsl) ** 2 for (eap,rsl) in zip(eap_vectors, rsl_vectors)]
        swath_ids = self.get_swath_id_vectors(pol)
        pg_swaths = self.get_pg_product(pol)
        for i, gtot in enumerate(tg_vectors):
            for j in range(1,6):
                gpi = swath_ids[i] == j
                gtot[gpi] *= pg_swaths[f'{self.obsMode}{j}'][i]
        return tg_vectors

    def get_tg_scales_offsets(self):
        """ Read scales and offsets of PG-based noise vectors from denosing_parameters.json """
        params = self.load_denoising_parameters_json()
        tg_id = f'{os.path.basename(self.filename)[:16]}_APG_{self.IPFversion:04.2f}'
        p = params[tg_id]
        offsets = []
        scales = []
        for i in range(5):
            offsets.append(p['B'][i*2] / p['Y_SCALE'])
            scales.append(p['B'][1+i*2] * p['A_SCALE'] / p['Y_SCALE'])
        return scales, offsets

    def get_swath_interpolator(self, pol, swath_name, line, pixel, z):
        """ Prepare interpolators for one swath """
        swathBound = self.swath_bounds(pol)[swath_name]
        swath_coords = (
            swathBound['firstAzimuthLine'],
            swathBound['lastAzimuthLine'],
            swathBound['firstRangeSample'],
            swathBound['lastRangeSample'],
        )
        pix_vec_fr = np.arange(min(swathBound['firstRangeSample']),
                               max(swathBound['lastRangeSample'])+1)

        z_vecs = []
        swath_lines = []
        for fal, lal, frs, lrs in zip(*swath_coords):
            valid1 = np.where((line >= fal) * (line <= lal))[0]
            if valid1.size == 0:
                continue
            for v1 in valid1:
                swath_lines.append(line[v1])
                valid2 = np.where(
                    (pixel[v1] >= frs) *
                    (pixel[v1] <= lrs) *
                    np.isfinite(z[v1]))[0]
                if valid2.size == 0:
                    z_vecs.append(np.zeros(pix_vec_fr.shape) + np.nan)
                else:
                    # interpolator for one line
                    z_interp1 = InterpolatedUnivariateSpline(pixel[v1][valid2], z[v1][valid2])
                    z_vecs.append(z_interp1(pix_vec_fr))
        # interpolator for one subswath
        z_interp2 = RectBivariateSpline(swath_lines, pix_vec_fr, np.array(z_vecs))
        return z_interp2, swath_coords
    
    def geolocation_interpolator(self, pol, z):
        """ Interpolator of data from geolocation XML """
        return RectBivariateSpline(
                self.geolocation(pol)['line'],
                self.geolocation(pol)['pixel'],
                z)

    def get_angle_vectors(self, pol, angle_name):
        """ Get vectors of angles from geolocations for pixels of range noise vectors """
        z = self.geolocation(pol)[angle_name]
        i = self.geolocation_interpolator(pol, z)
        angle_vectors = [
            i(l, p).flatten() for (l, p) in
            zip(self.noise_range(pol)['line'], self.noise_range(pol)['pixel'])
            ]
        return angle_vectors

    def get_calibration_vectors(self, pol, name='sigmaNought'):
        """ Get calibration constant for pixels of range noise vectors """
        line  = self.noise_range(pol)['line']
        pixel = self.noise_range(pol)['pixel']
        swath_names = ['%s%s' % (self.obsMode, iSW) for iSW in self.swath_ids]
        s0line = self.calibration(pol)['line']
        s0pixel = self.calibration(pol)['pixel']
        sigma0 = self.calibration(pol)[name]

        sigma0_vecs = [np.zeros_like(p_vec) + np.nan for p_vec in pixel]

        for swath_name in swath_names:
            sigma0interp, swath_coords = self.get_swath_interpolator(pol, swath_name, s0line, s0pixel, sigma0)

            for fal, lal, frs, lrs in zip(*swath_coords):
                valid1 = np.where((line >= fal) * (line <= lal))[0]
                if valid1.size == 0:
                    continue
                for v1 in valid1:
                    valid2 = np.where(
                        (pixel[v1] >= frs) *
                        (pixel[v1] <= lrs))[0]
                    sigma0_vecs[v1][valid2] = sigma0interp(line[v1], pixel[v1][valid2])        
        return sigma0_vecs

    def calibrate_noise_vectors(self, noise, cal_s0, scall):
        """ Compute calibrated NESZ from input noise, sigma0 calibration and scalloping noise"""
        return [s * n / c**2 for n, c, s in zip(noise, cal_s0, scall)]

    def get_eap_interpolator(self, subswathID, pol):
        """
        Prepare interpolator for Elevation Antenna Pattern.
        It computes EAP for input boresight angles

        """
        eap_lut = self.aux_calibration_params()[pol][subswathID]['elevationAntennaPattern']
        recordLength = self.aux_calibration_params()[pol][subswathID]['elevationAntennaPatternCount']
        eai_lut = self.aux_calibration_params()[pol][subswathID]['elevationAngleIncrement']
        if recordLength == eap_lut.shape[0]:
            # in case if elevationAntennaPattern is given in dB
            amplitudeLUT = 10**(eap_lut/10)
        else:
            amplitudeLUT = np.sqrt(eap_lut[0:-1:2]**2 + eap_lut[1::2]**2)
            
        angleLUT = np.arange(-(recordLength//2),+(recordLength//2)+1) * eai_lut        
        eap_interpolator = InterpolatedUnivariateSpline(angleLUT, np.sqrt(amplitudeLUT))
        return eap_interpolator

    
    def get_boresight_angle_interpolator(self, pol):
        """ Prepare interpolator for boresight angles. It computes BA for input x,y coordinates. """
        antennaPattern = self.antenna_pattern(pol)
        relativeAzimuthTime = []
        for iSW in self.swath_ids:
            subswathID = '%s%s' % (self.obsMode, iSW)
            relativeAzimuthTime.append([ (t-self.time_coverage_center).total_seconds()
                                         for t in antennaPattern[subswathID]['azimuthTime'] ])
        relativeAzimuthTime = np.hstack(relativeAzimuthTime)
        sortIndex = np.argsort(relativeAzimuthTime)
        rollAngle = []
        for iSW in self.swath_ids:
            subswathID = '%s%s' % (self.obsMode, iSW)
            rollAngle.append(antennaPattern[subswathID]['roll'])
        relativeAzimuthTime = np.hstack(relativeAzimuthTime)
        rollAngle = np.hstack(rollAngle)
        rollAngleIntp = InterpolatedUnivariateSpline(relativeAzimuthTime[sortIndex], rollAngle[sortIndex])
        roll_map = rollAngleIntp(self.geolocation_relative_azimuth_time(pol))

        boresight_map = self.geolocation(pol)['elevationAngle'] - roll_map
        boresight_angle_interpolator = self.geolocation_interpolator(pol, boresight_map)
        return boresight_angle_interpolator

    def get_range_spread_loss_interpolator(self, pol, rsl_power=3./2.):
        """ Prepare interpolator for Range Spreading Loss. It computes RSL for input x,y coordinates.
        rsl_power : float power for RSL = (2 * Rref / time / C)^rsl_power

        """
        referenceRange = float(self.xml.annotation[pol].find('referenceRange').text)
        rangeSpreadingLoss = (referenceRange / self.geolocation(pol)['slantRangeTime'] / SPEED_OF_LIGHT * 2)**rsl_power
        rsp_interpolator = self.geolocation_interpolator(pol, rangeSpreadingLoss)
        return rsp_interpolator

    def get_shifted_noise_vectors(self, pol, pixel=None, noise=None, skip=4, min_valid_size=10, rsl_power=3./2.):
        """
        Estimate shift in range noise LUT relative to antenna gain pattern and correct for it.

        """
        if pixel is None:
            pixel = self.noise_range(pol)['pixel']
        if noise is None:
            noise = self.noise_range(pol)['noise']
        line = self.noise_range(pol)['line']
        noise_shifted = [np.zeros(p.size) for p in pixel]
        # noise lut shift
        for swid in self.swath_ids:
            swath_name = f'{self.obsMode}{swid}'
            swathBound = self.swath_bounds(pol)[swath_name]
            eap_interpolator = self.get_eap_interpolator(swath_name, pol)
            ba_interpolator = self.get_boresight_angle_interpolator(pol)
            rsp_interpolator = self.get_range_spread_loss_interpolator(pol, rsl_power=rsl_power)
            zipped = zip(
                swathBound['firstAzimuthLine'],
                swathBound['lastAzimuthLine'],
                swathBound['firstRangeSample'],
                swathBound['lastRangeSample'],
            )
            for fal, lal, frs, lrs in zipped:
                valid1 = np.where((line >= fal) * (line <= lal))[0]
                for v1 in valid1:
                    valid_lin = line[v1]
                    # keep only pixels from that swath
                    valid2 = np.where(
                        (pixel[v1] >= frs) *
                        (pixel[v1] <= lrs) *
                        (np.isfinite(noise[v1])))[0]
                    if valid2.size >= min_valid_size:
                        # keep only unique pixels
                        valid_pix, valid_pix_i = np.unique(pixel[v1][valid2], return_index=True)
                        valid2 = valid2[valid_pix_i]

                        ba = ba_interpolator(valid_lin, valid_pix).flatten()
                        eap = eap_interpolator(ba).flatten()
                        rsp = rsp_interpolator(valid_lin, valid_pix).flatten()
                        apg = (1/eap/rsp)**2

                        noise_valid = np.array(noise[v1][valid2])
                        if np.allclose(noise_valid, noise_valid[0]):
                            noise_shifted[v1][valid2] = noise_valid
                        else:
                            noise_interpolator = InterpolatedUnivariateSpline(valid_pix, noise_valid)
                            pixel_shift = minimize(cost, 0, args=(valid_pix[skip:-skip], noise_interpolator, apg[skip:-skip])).x[0]
                            noise_shifted0 = noise_interpolator(valid_pix + pixel_shift)
                            noise_shifted[v1][valid2] = noise_shifted0
        return noise_shifted

    def get_corrected_noise_vectors(self, pol, nesz, pixel=None, add_pb=True):
        """ Load scaling and offset coefficients from files and apply to input  NESZ """
        if pixel is None:
            pixel = self.noise_range(pol)['pixel']
        line  = self.noise_range(pol)['line']
        nesz_corrected = [np.zeros(p.size)+np.nan for p in pixel]
        ns, pb = self.import_denoisingCoefficients(pol)[:2]
        for swid in self.swath_ids:
            swath_name = f'{self.obsMode}{swid}'
            swathBound = self.swath_bounds(pol)[swath_name]
            zipped = zip(
                swathBound['firstAzimuthLine'],
                swathBound['lastAzimuthLine'],
                swathBound['firstRangeSample'],
                swathBound['lastRangeSample'],
            )
            for fal, lal, frs, lrs in zipped:
                valid1 = np.where((line >= fal) * (line <= lal))[0]
                for v1 in valid1:
                    valid2 = np.where((pixel[v1] >= frs) * (pixel[v1] <= lrs))[0]
                    nesz_corrected[v1][valid2] = nesz[v1][valid2] * ns[swath_name]
                    if add_pb:
                        nesz_corrected[v1][valid2] += pb[swath_name]
        return nesz_corrected

    def get_raw_sigma0_vectors(self, pol, cal_s0, average_lines=111):
        """ Read DN_ values from input GeoTIff for a given lines, average in azimuth direction,
        compute sigma0, and return sigma0 for given pixels

        """
        line = self.noise_range(pol)['line']
        pixel = self.noise_range(pol)['pixel']
        ws2 = np.floor(average_lines / 2)
        raw_sigma0 = [np.zeros(p.size)+np.nan for p in pixel]
        src_filename = self.bands()[self.get_band_number(f'DN_{pol}')]['SourceFilename']
        ds = gdal.Open(src_filename)
        img_data = ds.ReadAsArray().astype(float)
        img_data[img_data == 0] = np.nan
        for i in range(line.shape[0]):
            y0 = max(0, line[i]-ws2)
            y1 = min(ds.RasterYSize, line[i]+ws2)
            line_data = img_data[int(y0):int(y1)]
            dn_mean = np.nanmean(line_data, axis=0)
            raw_sigma0[i] = dn_mean[pixel[i]]**2 / cal_s0[i]**2
        return raw_sigma0

    def get_raw_sigma0_vectors_from_full_size(self, line, pixel, swath_ids, sigma0_fs, wsy=20, wsx=5, avg_func=np.nanmean):
        """ Get values of sigma0 from input array on the pixels of noise range vectors. Averaging in range and azimuth. """
        raw_sigma0 = [np.zeros(p.size)+np.nan for p in pixel]
        for i in range(line.shape[0]):
            y0 = max(0, line[i]-wsy)
            y1 = min(sigma0_fs.shape[0], line[i]+wsy)+1
            if wsx == 0:
                raw_sigma0[i] = np.nanmean(sigma0_fs[int(y0):int(y1)], axis=0)[pixel[i]]
            else:
                for j in range(1,6):
                    gpi = swath_ids[i] == j
                    s0_gpi = []
                    for p in pixel[i][gpi]:
                        x0 = max(pixel[i][gpi].min(), p-wsx)
                        x1 = min(pixel[i][gpi].max(), p+wsx)+1
                        s0_window = sigma0_fs[int(y0):int(y1), int(x0):int(x1)]
                        s0_gpi.append(avg_func(s0_window))
                    raw_sigma0[i][gpi] = s0_gpi
        return raw_sigma0

    def get_vectors_from_full_size(self, line, pixel, array):
        """ Read array from input GeoTIff for given lines and for given pixels
            from full size longitude matrix

        """
        array_vecs = [np.zeros(p.size)+np.nan for p in pixel]
        for i in range(line.shape[0]):
            array_vecs[i] = array[line[i]][pixel[i]]
        return array_vecs

    def compute_rqm(self, s0, pol, num_px=100, **kwargs):
        """ Compute Range Quality Metric from the input sigma0 """
        line = self.noise_range(pol)['line']
        pixel = self.noise_range(pol)['pixel']
        q_all = {}
        for swid in self.swath_ids[:-1]:
            q_subswath = []
            swath_name = f'{self.obsMode}{swid}'
            swathBound = self.swath_bounds(pol)[swath_name]
            zipped = zip(
                swathBound['firstAzimuthLine'],
                swathBound['lastAzimuthLine'],
                swathBound['firstRangeSample'],
                swathBound['lastRangeSample'],
            )
            for fal, lal, frs, lrs in zipped:
                valid1 = np.where((line >= fal) * (line <= lal))[0]
                for v1 in valid1:
                    valid2a = np.where((pixel[v1] >= lrs-num_px) * (pixel[v1] <= lrs))[0]
                    valid2b = np.where((pixel[v1] >= lrs+1) * (pixel[v1] <= lrs+num_px+1))[0]
                    s0a = s0[v1][valid2a]
                    s0b = s0[v1][valid2b]
                    s0am = np.nanmean(s0a)
                    s0bm = np.nanmean(s0b)
                    s0as = np.nanstd(s0a)
                    s0bs = np.nanstd(s0a)
                    q = np.abs(s0am - s0bm) / (s0as + s0bs)
                    q_subswath.append([q, s0am, s0bm, s0as, s0bs, line[v1]])
            q_all[swath_name] = np.array(q_subswath)
        return q_all

    def get_range_quality_metric(self, pol='HV', **kwargs):
        """ Compute sigma0 with four methods (ESA, SHIFTED, NERSC, TG), compute RQM for each sigma0 """
        cal_s0 = self.get_calibration_vectors(pol)

        if self.IPFversion < 2.9:
            scall_esa = [np.ones(p.size) for p in self.noise_range(pol)['pixel']]
        else:
            scall_esa = self.get_noise_azimuth_vectors(pol)
        nesz_esa = self.calibrate_noise_vectors(self.noise_range(pol)['noise'], cal_s0, scall_esa)
        scall = self.get_noise_azimuth_vectors(pol)
        noise_shifted = self.get_shifted_noise_vectors(pol)
        nesz_shifted = self.calibrate_noise_vectors(noise_shifted, cal_s0, scall)
        nesz_corrected = self.get_corrected_noise_vectors(pol, nesz_shifted)
        noise_tg = self.get_noise_tg_vectors(pol)
        nesz_tg = self.calibrate_noise_vectors(noise_tg, cal_s0, scall)
        sigma0 = self.get_raw_sigma0_vectors(pol, cal_s0)
        s0_esa   = [s0 - n0 for (s0,n0) in zip(sigma0, nesz_esa)]
        s0_shift = [s0 - n0 for (s0,n0) in zip(sigma0, nesz_shifted)]
        s0_nersc = [s0 - n0 for (s0,n0) in zip(sigma0, nesz_corrected)]
        s0_tg   = [s0 - n0 for (s0,n0) in zip(sigma0, nesz_tg)]
        q = [self.compute_rqm(s0, pol) for s0 in [s0_esa, s0_shift, s0_nersc, s0_tg]]
        alg_names = ['ESA', 'SHIFT', 'NERSC', 'TG']
        var_names = ['RQM', 'AVG1', 'AVG2', 'STD1', 'STD2']
        q_all = {'IPF': self.IPFversion}
        for swid in self.swath_ids[:-1]:
            swath_name = f'{self.obsMode}{swid}'
            for alg_i, alg_name in enumerate(alg_names):
                for var_i, var_name in enumerate(var_names):
                    q_all[f'{var_name}_{swath_name}_{alg_name}'] = list(q[alg_i][swath_name][:, var_i])
            q_all[f'LINE_{swath_name}'] = list(q[alg_i][swath_name][:, 5])
        return q_all

    def experiment_get_data(self, pol, average_lines, zoom_step):
        """ Prepare data for  noiseScaling and powerBalancing experiments """
        crop = {'IW':400, 'EW':200}[self.obsMode]
        pixel0 = self.noise_range['pixel']
        noise0 = self.noise_range['noise']
        cal_s00 = self.get_calibration_vectors(pol)
        # zoom:
        pixel = [np.arange(p[0], p[-1], zoom_step) for p in pixel0]
        noise = [interp1d(p, n)(p2) for (p,n,p2) in zip(pixel0, noise0, pixel)]
        cal_s0 = [interp1d(p, n)(p2) for (p,n,p2) in zip(pixel0, cal_s00, pixel)]

        noise_shifted = self.get_shifted_noise_vectors(pol, pixel, noise)
        scall = self.get_noise_azimuth_vectors(pol, pixel)
        nesz = self.calibrate_noise_vectors(noise_shifted, cal_s0, scall)
        sigma0_fs = self.get_raw_sigma0_full_size(pol)
        line = self.noise_range(pol)['line']
        sigma0 = self.get_raw_sigma0_vectors_from_full_size(
            pol, line, pixel, sigma0_fs, average_lines=average_lines)
        return line, pixel, sigma0, nesz, crop, self.swath_bounds(pol)

    def experiment_noiseScaling(self, pol, average_lines=777, zoom_step=2):
        """ Compute noise scaling coefficients for each range noise line and save as NPZ """
        line, pixel, sigma0, nesz, crop, swathBounds = self.experiment_get_data(
            pol, average_lines, zoom_step)

        results = {}
        results['IPFversion'] = self.IPFversion
        for swid in self.swath_ids:
            swath_name = f'{self.obsMode}{swid}'
            results[swath_name] = {
                'sigma0':[],
                'noiseEquivalentSigma0':[],
                'scalingFactor':[],
                'correlationCoefficient':[],
                'fitResidual':[] }
            swathBound = swathBounds[swath_name]
            zipped = zip(
                swathBound['firstAzimuthLine'],
                swathBound['lastAzimuthLine'],
                swathBound['firstRangeSample'],
                swathBound['lastRangeSample'],
            )
            for fal, lal, frs, lrs in zipped:
                valid1 = np.where(
                    (line >= fal) *
                    (line <= lal) *
                    (line > (average_lines / 2)) *
                    (line < (line[-1] - average_lines / 2)))[0]
                for v1 in valid1:
                    valid2 = np.where(
                        (pixel[v1] >= frs+crop) *
                        (pixel[v1] <= lrs-crop) *
                        np.isfinite(nesz[v1]))[0]
                    meanS0 = sigma0[v1][valid2]
                    meanN0 = nesz[v1][valid2]
                    pixelIndex = pixel[v1][valid2]
                    (scalingFactor,
                     correlationCoefficient,
                     fitResidual) = fit_noise_scaling_coeff(meanS0, meanN0, pixelIndex)
                    results[swath_name]['sigma0'].append(meanS0)
                    results[swath_name]['noiseEquivalentSigma0'].append(meanN0)
                    results[swath_name]['scalingFactor'].append(scalingFactor)
                    results[swath_name]['correlationCoefficient'].append(correlationCoefficient)
                    results[swath_name]['fitResidual'].append(fitResidual)
        np.savez(self.filename.split('.')[0] + '_noiseScaling.npz', **results)

    def experiment_powerBalancing(self, pol, average_lines=777, zoom_step=2):
        """ Compute power balancing coefficients for each range noise line and save as NPZ """
        line, pixel, sigma0, nesz, crop, swathBounds = self.experiment_get_data(pol, average_lines, zoom_step)
        nesz_corrected = self.get_corrected_noise_vectors(pol, nesz, pixel=pixel, add_pb=False)

        num_swaths = len(self.swath_ids)
        swath_names = ['%s%s' % (self.obsMode, iSW) for iSW in self.swath_ids]

        results = {}
        results['IPFversion'] = self.IPFversion
        tmp_results = {}
        for swath_name in swath_names:
            results[swath_name] = {
                'sigma0':[],
                'noiseEquivalentSigma0':[],
                'correlationCoefficient':[],
                'fitResidual':[],
                'balancingPower': []}
            tmp_results[swath_name] = {}

        valid_lines = np.where(
            (line > (average_lines / 2)) *
            (line < (line[-1] - average_lines / 2)))[0]
        for li in valid_lines:
            # find frs, lrs for all swaths at this line
            frs = {}
            lrs = {}
            for swath_name in swath_names:
                swathBound = swathBounds[swath_name]
                zipped = zip(
                    swathBound['firstAzimuthLine'],
                    swathBound['lastAzimuthLine'],
                    swathBound['firstRangeSample'],
                    swathBound['lastRangeSample'],
                )
                for fal, lal, frstmp, lrstmp in zipped:
                    if line[li] >= fal and line[li] <= lal:
                        frs[swath_name] = frstmp
                        lrs[swath_name] = lrstmp
                        break

            if swath_names != sorted(list(frs.keys())):
                continue

            blockN0 = np.zeros(nesz[li].shape) + np.nan
            blockRN0 = np.zeros(nesz[li].shape) + np.nan
            valid2_zero_size = False
            fitCoefficients = []
            for swath_name in swath_names:
                swathBound = swathBounds[swath_name]
                valid2 = np.where(
                    (pixel[li] >= frs[swath_name]+crop) *
                    (pixel[li] <= lrs[swath_name]-crop) *
                    np.isfinite(nesz[li]))[0]
                if valid2.size == 0:
                    valid2_zero_size = True
                    break
                meanS0 = sigma0[li][valid2]
                meanN0 = nesz_corrected[li][valid2]
                blockN0[valid2] = nesz_corrected[li][valid2]
                meanRN0 = nesz[li][valid2]
                blockRN0[valid2] = nesz[li][valid2]
                pixelIndex = pixel[li][valid2]
                fitResults = np.polyfit(pixelIndex, meanS0 - meanN0, deg=1, full=True)
                fitCoefficients.append(fitResults[0])
                tmp_results[swath_name]['sigma0'] = meanS0
                tmp_results[swath_name]['noiseEquivalentSigma0'] = meanRN0
                tmp_results[swath_name]['correlationCoefficient'] = np.corrcoef(meanS0, meanN0)[0,1]
                tmp_results[swath_name]['fitResidual'] = fitResults[1].item()

            if valid2_zero_size:
                continue

            if np.any(np.isnan(fitCoefficients)):
                continue

            balancingPower = np.zeros(num_swaths)
            for i in range(num_swaths - 1):
                swath_name = f'{self.obsMode}{i+1}'
                swathBound = swathBounds[swath_name]
                power1 = fitCoefficients[i][0] * lrs[swath_name] + fitCoefficients[i][1]
                # Compute power right to a boundary as slope*interswathBounds + residual coef.
                power2 = fitCoefficients[i+1][0] * lrs[swath_name] + fitCoefficients[i+1][1]
                balancingPower[i+1] = power2 - power1
            balancingPower = np.cumsum(balancingPower)

            for iSW, swath_name in zip(self.swath_ids, swath_names):
                swathBound = swathBounds[swath_name]
                valid2 = np.where(
                    (pixel[li] >= frs[swath_name]+crop) *
                    (pixel[li] <= lrs[swath_name]-crop) *
                    np.isfinite(nesz[li]))[0]
                blockN0[valid2] += balancingPower[iSW-1]

            valid3 = (pixel[li] >= frs[f'{self.obsMode}2'] + crop)
            powerBias = np.nanmean((blockRN0-blockN0)[valid3])
            balancingPower += powerBias

            for iSW, swath_name in zip(self.swath_ids, swath_names):
                tmp_results[swath_name]['balancingPower'] = balancingPower[iSW-1]

            for swath_name in swath_names:
                for key in tmp_results[swath_name]:
                    results[swath_name][key].append(tmp_results[swath_name][key])

        np.savez(self.filename.split('.')[0] + '_powerBalancing.npz', **results)

    def interp_nrv_full_size(self, z, line, pixel, pol, power=1):
        """ Interpolate noise range vectors to full size """
        z_fs = np.zeros(self.shape(pol)) + np.nan
        swath_names = [f'{self.obsMode}{i}' for i in self.swath_ids]
        for swath_name in swath_names:
            z_interp2, swath_coords = self.get_swath_interpolator(pol, swath_name, line, pixel, z)
            for fal, lal, frs, lrs in zip(*swath_coords):
                pix_vec_fr = np.arange(frs, lrs+1)
                lin_vec_fr = np.arange(fal, lal+1)
                z_arr_fs = z_interp2(lin_vec_fr, pix_vec_fr)
                if power != 1:
                    z_arr_fs = z_arr_fs**power
                z_fs[fal:lal+1, frs:lrs+1] = z_arr_fs
        return z_fs

    def get_corrected_nesz_full_size(self, pol, nesz):
        """ Get corrected NESZ on full size matrix """
        nesz_corrected = np.array(nesz)
        ns, pb = self.import_denoisingCoefficients(pol)[:2]
        for swid in self.swath_ids:
            swath_name = f'{self.obsMode}{swid}'
            # skip correction id NS/PB coeffs are not available (e.g. HH or VV)
            if swath_name not in ns:
                continue
            swathBound = self.swath_bounds(pol)[swath_name]
            zipped = zip(
                swathBound['firstAzimuthLine'],
                swathBound['lastAzimuthLine'],
                swathBound['firstRangeSample'],
                swathBound['lastRangeSample'],
            )
            for fal, lal, frs, lrs in zipped:
                nesz_corrected[fal:lal+1, frs:lrs+1] *= ns[swath_name]
                nesz_corrected[fal:lal+1, frs:lrs+1] += pb[swath_name]
        return nesz_corrected

    def get_raw_sigma0_full_size(self, pol, min_dn=0):
        """ Read DN from input GeoTiff file and calibrate """
        src_filename = self.measurements[pol]
        ds = gdal.Open(src_filename)
        dn = ds.ReadAsArray()

        sigma0_fs = dn.astype(float)**2 / self.interp_nrv_full_size(
            self.calibration(pol)['sigmaNought'],
            self.calibration(pol)['line'],
            self.calibration(pol)['pixel'],
            pol, power=2)
        sigma0_fs[dn <= min_dn] = np.nan
        return sigma0_fs

    def export_noise_xml(self, pol, output_path):
        """ Export corrected (shifted and scaled) range noise into XML file """
        crosspol_noise_file = [fn for fn in glob.glob(self.filename+'/annotation/calibration/*')
                          if 'noise-s1' in fn and '-%s-' % pol.lower() in fn][0]

        noise1 = self.get_shifted_noise_vectors(pol)
        noise2 = self.get_corrected_noise_vectors(pol, noise1)
        tree = ET.parse(crosspol_noise_file)
        root = tree.getroot()
        for noiseRangeVector, pixel_vec, noise_vec in zip(root.iter('noiseRangeVector'), self.noise_range(pol)['pixel'], noise2):
            noise_vec[np.isnan(noise_vec)] = 0
            noiseRangeVector.find('pixel').text = ' '.join([f'{p}' for p in list(pixel_vec)])
            noiseRangeVector.find('noiseRangeLut').text = ' '.join([f'{p}' for p in list(noise_vec)])
        tree.write(os.path.join(output_path, os.path.basename(crosspol_noise_file)))
        return crosspol_noise_file

    def get_noise_tg_vectors(self, pol):
        """ Compute noise using total gain vectors and precomputed scales and offsets """
        pixel = self.noise_range(pol)['pixel']
        noise = [np.zeros_like(i) for i in pixel]
        scales, offsets = self.get_tg_scales_offsets()
        gtot = self.get_tg_vectors(pol)
        swath_ids = self.get_swath_id_vectors(pol)
        for i in range(len(gtot)):
            for j in range(1,6):
                gpi = swath_ids[i] == j
                noise[i][gpi] = offsets[j-1] + gtot[i][gpi] * scales[j-1]
        return noise

    def get_nesz_full_size(self, pol, algorithm):
        """ Create matrix of NESZ in full resolution for entire scene """
        # ESA noise vectors
        noise = self.noise_range(pol)['noise']
        if algorithm == 'NERSC':
            # NERSC correction of noise shift in range direction
            noise = self.get_shifted_noise_vectors(pol)
        elif algorithm == 'NERSC_TG':
            # Total Gain - based noise vectors
            noise = self.get_noise_tg_vectors(pol)
        # noise calibration
        cal_s0 = self.get_calibration_vectors(pol)
        nesz = [n / c**2 for (n,c) in zip(noise, cal_s0)]
        # noise full size
        nesz_fs = self.interp_nrv_full_size(
            nesz,
            self.noise_range(pol)['line'], 
            self.noise_range(pol)['pixel'],
            pol)
        # scalloping correction
        nesz_fs *= self.get_scalloping_full_size(pol)
        # NERSC correction of NESZ magnitude
        if algorithm == 'NERSC':
            nesz_fs = self.get_corrected_nesz_full_size(pol, nesz_fs)
        return nesz_fs

    def remove_thermal_noise(self, pol, algorithm='NERSC', remove_negative=True, min_dn=0):
        """ Get calibrated sigma0 and subtract NESZ """
        nesz_fs = self.get_nesz_full_size(pol, algorithm)
        sigma0 = self.get_raw_sigma0_full_size(pol, min_dn=min_dn)
        sigma0 -= nesz_fs

        if remove_negative:
            sigma0 = fill_gaps(sigma0, sigma0 <= 0)
        return sigma0

    @lru_cache(maxsize=None)
    def antenna_pattern(self, pol):
        """ Read antennaPattern from annotation XML """
        list_keys = ['slantRangeTime', 'elevationAngle', 'elevationPattern', 'incidenceAngle']
        antenna_pattern = {}
        antennaPatternList = self.xml.annotation[pol].find('antennaPatternList')
        compute_roll = True
        for antennaPattern in antennaPatternList.find_all('antennaPattern'):
            swath = antennaPattern.swath.text
            if swath not in antenna_pattern:
                antenna_pattern[swath] = defaultdict(list)
            antenna_pattern[swath]['azimuthTime'].append(parse_azimuth_time(antennaPattern.azimuthTime.text))
            for list_key in list_keys:
                antenna_pattern[swath][list_key].append(np.array([float(i) for i in antennaPattern.find(list_key).text.split()]))
            antenna_pattern[swath]['terrainHeight'].append(float(antennaPattern.terrainHeight.text))
            if antennaPattern.find('roll'):
                antenna_pattern[swath]['roll'].append(float(antennaPattern.roll.text))
                compute_roll = False
        if compute_roll:
            for swath in antenna_pattern:
                antenna_pattern[swath]['roll'] = self.compute_roll(pol, antenna_pattern[swath])
        return antenna_pattern

    @lru_cache(maxsize=None)
    def import_orbit(self, pol):
        ''' Import orbit information from annotation XML DOM '''
        orbit = { 'time':[],
                  'position':{'x':[], 'y':[], 'z':[]},
                  'velocity':{'x':[], 'y':[], 'z':[]} }
        for o in self.xml.annotation[pol].find('orbitList').find_all('orbit'):
            orbit['time'].append(parse_azimuth_time(o.time.text))
            for name1 in ['position', 'velocity']:
                for name2 in ['x', 'y', 'z']:
                    orbit[name1][name2].append(float(o.find(name1).find(name2).text))
        return orbit

    def orbitAtGivenTime(self, pol, relativeAzimuthTime):
        ''' Interpolate orbit parameters for given time vector '''
        stateVectors = self.import_orbit(pol)
        orbitTime = np.array([ (t-self.time_coverage_center).total_seconds()
                                for t in stateVectors['time'] ])
        orbitAtGivenTime = { 'relativeAzimuthTime':relativeAzimuthTime,
                             'positionXYZ':[],
                             'velocityXYZ':[] }
        for t in relativeAzimuthTime:
            useIndices = sorted(np.argsort(abs(orbitTime-t))[:4])
            for k in ['position', 'velocity']:
                orbitAtGivenTime[k+'XYZ'].append([
                    cubic_hermite_interpolation(orbitTime[useIndices],
                        np.array(stateVectors[k][component])[useIndices], t)
                    for component in ['x','y','z'] ])
        for k in ['positionXYZ', 'velocityXYZ']:
            orbitAtGivenTime[k] = np.squeeze(orbitAtGivenTime[k])
        return orbitAtGivenTime

    def compute_roll(self, pol, antenna_pattern):
        """ Compute roll angle from antenna_pattern """
        relativeAzimuthTime = np.array([
            (t - self.time_coverage_center).total_seconds()
            for t in antenna_pattern['azimuthTime']
        ])
        positionXYZ = self.orbitAtGivenTime(pol, relativeAzimuthTime)['positionXYZ']
        satelliteLatitude = np.arctan2(positionXYZ[:,2], np.sqrt(positionXYZ[:,0]**2 + positionXYZ[:,1]**2))
        r_major = 6378137.0            # WGS84 semi-major axis
        r_minor = 6356752.314245179    # WGS84 semi-minor axis
        earthRadius = np.sqrt(  (  (r_major**2 * np.cos(satelliteLatitude))**2
                                + (r_minor**2 * np.sin(satelliteLatitude))**2)
                            / (  (r_major * np.cos(satelliteLatitude))**2
                                + (r_minor * np.sin(satelliteLatitude))**2) )
        satelliteAltitude = np.linalg.norm(positionXYZ, axis=1) - earthRadius
        # see Eq.9-19 in the reference R2.
        rollAngle = 29.45 - 0.0566*(satelliteAltitude/1000. - 711.7)
        return rollAngle

    def load_denoising_parameters_json(self):
        """ Load scale and offset for NERSC algorithm from JSON file """
        denoise_filename = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            'denoising_parameters.json')
        with open(denoise_filename) as f:
            params = json.load(f)
        return params

    def import_denoisingCoefficients(self, pol, load_extra_scaling=False):
        ''' Import denoising coefficients '''
        filename_parts = os.path.basename(self.filename).split('_')
        platform = filename_parts[0]
        mode = filename_parts[1]
        resolution = filename_parts[2]
        params = self.load_denoising_parameters_json()

        noiseScalingParameters = {}
        powerBalancingParameters = {}
        extraScalingParameters = {}
        noiseVarianceParameters = {}
        IPFversion = float(self.IPFversion)
        sensingDate = datetime.strptime(self.filename.split(os.sep)[-1].split('_')[4], '%Y%m%dT%H%M%S')
        if platform=='S1B' and IPFversion==2.72 and sensingDate >= datetime(2017,1,16,13,42,34):
            # Adaption for special case.
            # ESA abrubtly changed scaling LUT in AUX_PP1 from 20170116 while keeping the IPFv.
            # After this change, the scaling parameters seems be much closer to those of IPFv 2.8.
            IPFversion = 2.8

        base_key = f'{platform}_{mode}_{resolution}_{pol}'
        for iSW in self.swath_ids:
            subswathID = '%s%s' % (self.obsMode, iSW)

            ns_key = f'{base_key}_NS_%0.1f' % IPFversion
            if ns_key in params:
                noiseScalingParameters[subswathID] = params[ns_key].get(subswathID, 1)
            else:
                print(f'WARNING: noise scaling for {subswathID} (IPF:{IPFversion}) is missing.')
                noiseScalingParameters[subswathID] = 1

            pb_key = f'{base_key}_PB_%0.1f' % IPFversion
            if pb_key in params:
                powerBalancingParameters[subswathID] = params[pb_key].get(subswathID, 0)
            else:
                print(f'WARNING: power balancing for {subswathID} (IPF:{IPFversion}) is missing.')
                powerBalancingParameters[subswathID] = 0

            if not load_extra_scaling:
                continue
            es_key = f'{base_key}_ES_%0.1f' % IPFversion
            if es_key in params:
                extraScalingParameters[subswathID] = params[es_key][subswathID]
                extraScalingParameters['SNNR'] = params[es_key]['SNNR']
            else:
                print(f'WARNING: extra scaling for {subswathID} (IPF:{IPFversion}) is missing.')
                extraScalingParameters['SNNR'] = np.linspace(-30,+30,601)
                extraScalingParameters[subswathID] = np.ones(601)

            nv_key = f'{base_key}_NV_%0.1f' % IPFversion
            if pb_key in params:
                nv_key[subswathID] = params[nv_key].get(subswathID, 0)
            else:
                print(f'WARNING: noise variance for {subswathID} (IPF:{IPFversion}) is missing.')

        return ( noiseScalingParameters, powerBalancingParameters, extraScalingParameters,
                 noiseVarianceParameters )

    def remove_texture_noise(self, pol, window=3, weight=0.1, s0_min=0, remove_negative=True, algorithm='NERSC', min_dn=0, **kwargs):
        """ Thermal noise removal followed by textural noise compensation using Method2

        Method2 is implemented as a weighted average of sigma0 and sigma0 smoothed with
        a gaussian filter. Weight of sigma0 is proportional to SNR. Total noise power
        is preserved by ofsetting the output signal by mean noise. Values below <s0_min>
        are clipped to s0_min.

        Parameters
        ----------
        pol : str
            'HH' or 'HV' or 'or 'VV' 'VH'
        window : int
            Size of window in the gaussian filter
        weight : float
            Weight of smoothed signal
        s0_min : float
            Minimum value of sigma0 for clipping

        Returns
        -------
        sigma0 : 2d numpy.ndarray
            Full size array with thermal and texture noise removed

        """

        if self.IPFversion == 3.2:
            self.IPFversion = 3.1
        sigma0 = self.get_raw_sigma0_full_size(pol, min_dn=min_dn)
        nesz = self.get_nesz_full_size(pol, algorithm)
        s0_offset = np.nanmean(nesz)
        if s0_offset == 0:
            sigma0o = sigma0
        else:
            sigma0 -= nesz
            sigma0g = gaussian_filter(sigma0, window)
            snr = sigma0g / nesz
            sigma0o = (weight * sigma0g + snr * sigma0) / (weight + snr) + s0_offset

        if remove_negative and np.nanmin(sigma0o) < 0:
            sigma0o = fill_gaps(sigma0o, sigma0o <= s0_min)

        return sigma0o

    def subswathIndexMap(self, pol):
        ''' Convert subswath indices into full grid pixels '''
        subswathIndexMap = np.zeros(self.shape(pol), dtype=np.uint8)
        for iSW in range(1, {'IW':3, 'EW':5}[self.obsMode]+1):
            swathBound = self.swath_bounds(pol)['%s%s' % (self.obsMode, iSW)]
            zipped = zip(swathBound['firstAzimuthLine'],
                         swathBound['firstRangeSample'],
                         swathBound['lastAzimuthLine'],
                         swathBound['lastRangeSample'])
            for fal, frs, lal, lrs in zipped:
                subswathIndexMap[fal:lal+1,frs:lrs+1] = iSW
        return subswathIndexMap
    
    def subswathCenterSampleIndex(self, pol):
        ''' Range center pixel indices along azimuth for each subswath '''
        swathBounds = self.swath_bounds(pol)
        subswathCenterSampleIndex = {}
        for iSW in range(1, {'IW':3, 'EW':5}[self.obsMode]+1):
            subswathID = '%s%s' % (self.obsMode, iSW)
            numberOfLines = (   np.array(swathBounds[subswathID]['lastAzimuthLine'])
                              - np.array(swathBounds[subswathID]['firstAzimuthLine']) + 1 )
            midPixelIndices = (   np.array(swathBounds[subswathID]['firstRangeSample'])
                                + np.array(swathBounds[subswathID]['lastRangeSample']) ) / 2.
            subswathCenterSampleIndex[subswathID] = int(round(
                np.sum(midPixelIndices * numberOfLines) / np.sum(numberOfLines) ))
        return subswathCenterSampleIndex
    
    def azimuthFmRateAtGivenTime(self, pol, relativeAzimuthTime, slantRangeTime):
        ''' Get azimuth frequency modulation rate for given time vectors

        Returns
        -------
        vector for all pixels in azimuth direction
        '''
        if relativeAzimuthTime.size != slantRangeTime.size:
            raise ValueError('relativeAzimuthTime and slantRangeTime must have the same dimension')
        azimuthFmRate = self.import_azimuthFmRate(pol)
        azimuthFmRatePolynomial = np.array(azimuthFmRate['azimuthFmRatePolynomial'])
        t0 = np.array(azimuthFmRate['t0'])
        xp = np.array([ (t-self.time_coverage_center).total_seconds()
                        for t in azimuthFmRate['azimuthTime'] ])
        azimuthFmRateAtGivenTime = []
        for tt in zip(relativeAzimuthTime,slantRangeTime):
            fp = (   azimuthFmRatePolynomial[:,0]
                   + azimuthFmRatePolynomial[:,1] * (tt[1]-t0)**1
                   + azimuthFmRatePolynomial[:,2] * (tt[1]-t0)**2 )
            azimuthFmRateAtGivenTime.append(np.interp(tt[0], xp, fp))
        return np.squeeze(azimuthFmRateAtGivenTime)
    
    def import_azimuthFmRate(self, pol):
        ''' Import azimuth frequency modulation rate from annotation XML DOM '''
        azimuthFmRate = defaultdict(list)
        for afmr in self.xml.annotation[pol].find_all('azimuthFmRate'):
            azimuthFmRate['azimuthTime'].append(datetime.strptime(afmr.azimuthTime.text, '%Y-%m-%dT%H:%M:%S.%f'))
            azimuthFmRate['t0'].append(float(afmr.t0.text))
            if 'azimuthFmRatePolynomial' in afmr.decode():
                afmrp = list(map(float, afmr.azimuthFmRatePolynomial.text.split(' ')))
            elif 'c0' in afmr.decode() and 'c1' in afmr.decode() and 'c2' in afmr.decode():
                afmrp = [float(afmr.c0.text), float(afmr.c1.text), float(afmr.c2.text)]
            azimuthFmRate['azimuthFmRatePolynomial'].append(afmrp)
        return azimuthFmRate
    
    @lru_cache(maxsize=None)
    def focusedBurstLengthInTime(self, pol):
        ''' Get focused burst length in zero-Doppler time domain

        Returns
        -------
        focusedBurstLengthInTime : dict
            one values for each subswath (different for IW and EW)
        '''
        azimuthFrequency = float(self.xml.annotation[pol].find('azimuthFrequency').text)
        azimuthTimeIntevalInSLC = 1. / azimuthFrequency
        focusedBurstLengthInTime = {}
        # nominalLinesPerBurst should be smaller than the real values
        nominalLinesPerBurst = {'IW':1450, 'EW':1100}[self.obsMode]
        for inputDimensions in self.xml.annotation[pol].find_all('inputDimensions'):
            swath = inputDimensions.swath.text
            numberOfInputLines = int(inputDimensions.numberOfInputLines.text)
            numberOfBursts = max(
                [ primeNumber for primeNumber in range(1,numberOfInputLines//nominalLinesPerBurst+1)
                  if (numberOfInputLines % primeNumber)==0 ] )
            if (numberOfInputLines % numberOfBursts)==0:
                focusedBurstLengthInTime[swath] = (
                    numberOfInputLines / numberOfBursts * azimuthTimeIntevalInSLC )
            else:
                raise ValueError('number of bursts cannot be determined.')
        return focusedBurstLengthInTime
    
    @lru_cache(maxsize=None)
    def scalloping_gain(self, pol, subswathID):
        """ Compute scalloping gain for old data """
        # azimuth antenna element patterns (AAEP) lookup table for given subswath
        gainAAEP = self.aux_calibration_params()[pol][subswathID]['azimuthAntennaPattern']
        azimuthAngleIncrement = self.aux_calibration_params()[pol][subswathID]['azimuthAngleIncrement']
        angleAAEP = np.arange(-(len(gainAAEP)//2), len(gainAAEP)//2+1) * azimuthAngleIncrement
        # subswath range center pixel index
        subswathCenterSampleIndex = self.subswathCenterSampleIndex(pol)[subswathID]
        # slant range time along subswath range center
        interpolator = self.geolocation_interpolator(pol, self.geolocation(pol)['slantRangeTime'])
        slantRangeTime = np.squeeze(interpolator(np.arange(self.shape(pol)[0]), subswathCenterSampleIndex))
        # relative azimuth time along subswath range center
        interpolator = self.geolocation_interpolator(pol, self.geolocation_relative_azimuth_time(pol))
        azimuthTime = np.squeeze(interpolator(np.arange(self.shape(pol)[0]), subswathCenterSampleIndex))
        # Doppler rate induced by satellite motion
        motionDopplerRate = self.azimuthFmRateAtGivenTime(pol, azimuthTime, slantRangeTime)
        # antenna steering rate
        antennaSteeringRate = np.deg2rad(ANTENNA_STEERING_RATE[subswathID])
        # satellite absolute velocity along subswath range center
        satelliteVelocity = np.linalg.norm(self.orbitAtGivenTime(pol, azimuthTime)['velocityXYZ'], axis=1)
        # Doppler rate induced by TOPS steering of antenna
        steeringDopplerRate = 2 * satelliteVelocity / RADAR_WAVELENGTH * antennaSteeringRate
        # combined Doppler rate (net effect)
        combinedDopplerRate = motionDopplerRate * steeringDopplerRate / (motionDopplerRate - steeringDopplerRate)
        # full burst length in zero-Doppler time
        fullBurstLength = self.focusedBurstLengthInTime(pol)[subswathID]
        # zero-Doppler azimuth time at each burst start
        burstStartTime = np.array([
            (t-self.time_coverage_center).total_seconds()
            for t in self.antenna_pattern(pol)[subswathID]['azimuthTime'] ])
        # burst overlapping length
        burstOverlap = fullBurstLength - np.diff(burstStartTime)
        burstOverlap = np.hstack([burstOverlap[0], burstOverlap])
        # time correction
        burstStartTime += burstOverlap / 2.
        # if burst start time does not cover the full image,
        # add more sample points using the closest burst length
        while burstStartTime[0] > azimuthTime[0]:
            burstStartTime = np.hstack(
                [burstStartTime[0] - np.diff(burstStartTime)[0], burstStartTime])
        while burstStartTime[-1] < azimuthTime[-1]:
            burstStartTime = np.hstack(
                [burstStartTime, burstStartTime[-1] + np.diff(burstStartTime)[-1]])
        # convert azimuth time to burst time
        burstTime = np.copy(azimuthTime)
        for li in range(len(burstStartTime)-1):
            valid = (   (azimuthTime >= burstStartTime[li])
                      * (azimuthTime < burstStartTime[li+1]) )
            burstTime[valid] -= (burstStartTime[li] + burstStartTime[li+1]) / 2.
        # compute antenna steering angle for each burst time
        antennaSteeringAngle = np.rad2deg(
            RADAR_WAVELENGTH / (2 * satelliteVelocity)
            * combinedDopplerRate * burstTime )
        # compute scalloping gain for each burst time
        burstAAEP = np.interp(antennaSteeringAngle, angleAAEP, gainAAEP)
        scallopingGain = 1. / 10**(burstAAEP/10.)
        return scallopingGain

    def get_noise_azimuth_vectors(self, pol, pixel=None):
        """ Interpolate scalloping noise from XML files to range noise line/pixel coords """
        if pixel is None:
            pixel = self.noise_range(pol)['pixel']
        line = self.noise_range(pol)['line']
        scall = [np.zeros(p.size) for p in pixel]
        if self.IPFversion < 2.9:
            swath_idvs = self.get_swath_id_vectors(pol, pixel)
            for i, l in enumerate(line):
                for j in range(1,6):
                    gpi = swath_idvs[i] == j
                    scall[i][gpi] = self.scalloping_gain(pol, f'{self.obsMode}{j}')[l]
            return scall
        noiseAzimuthVector = self.noise_azimuth(pol)
        for iSW in self.swath_ids:
            swath = f'{self.obsMode}{iSW}'
            numberOfBlocks = len(noiseAzimuthVector[swath]['firstAzimuthLine'])
            for iBlk in range(numberOfBlocks):
                frs = noiseAzimuthVector[swath]['firstRangeSample'][iBlk]
                lrs = noiseAzimuthVector[swath]['lastRangeSample'][iBlk]
                fal = noiseAzimuthVector[swath]['firstAzimuthLine'][iBlk]
                lal = noiseAzimuthVector[swath]['lastAzimuthLine'][iBlk]
                y = np.array(noiseAzimuthVector[swath]['line'][iBlk])
                z = np.array(noiseAzimuthVector[swath]['noise'][iBlk])
                if y.size > 1:
                    nav_interp = InterpolatedUnivariateSpline(y, z, k=1)
                else:
                    nav_interp = lambda x: z

                line_gpi = np.where((line >= fal) * (line <= lal))[0]
                for line_i in line_gpi:
                    pixel_gpi = np.where((pixel[line_i] >= frs) * (pixel[line_i] <= lrs))[0]
                    scall[line_i][pixel_gpi] = nav_interp(line[line_i])
        return scall

    def get_scalloping_full_size(self, pol):
        """ Interpolate noise azimuth vector to full resolution for all blocks """
        scall_fs = np.zeros(self.shape(pol))
        if self.IPFversion < 2.9:
            subswathIndexMap = self.subswathIndexMap(pol)
            for iSW in range(1, {'IW':3, 'EW':5}[self.obsMode]+1):
                subswathID = '%s%s' % (self.obsMode, iSW)
                scallopingGain = self.scalloping_gain(pol, subswathID)
                # assign computed scalloping gain into each subswath
                valid = (subswathIndexMap==iSW)
                scall_fs[valid] = (
                    scallopingGain[:, np.newaxis] * np.ones((1,self.shape(pol)[1])))[valid]
            return scall_fs
            
        noiseAzimuthVector = self.noise_azimuth(pol)
        swath_names = ['%s%s' % (self.obsMode, iSW) for iSW in self.swath_ids]
        for swath_name in swath_names:
            nav = noiseAzimuthVector[swath_name]
            zipped = zip(
                nav['firstAzimuthLine'],
                nav['lastAzimuthLine'],
                nav['firstRangeSample'],
                nav['lastRangeSample'],
                nav['line'],
                nav['noise'],
            )
            for fal, lal, frs, lrs, y, z in zipped:
                if isinstance(y, (list, np.ndarray)) and len(y) > 1:
                    nav_interp = InterpolatedUnivariateSpline(y, z, k=1)
                else:
                    nav_interp = lambda x: z
                lin_vec_fr = np.arange(fal, lal+1)
                z_vec_fr = nav_interp(lin_vec_fr)
                z_arr = np.repeat([z_vec_fr], (lrs-frs+1), axis=0).T
                scall_fs[fal:lal+1, frs:lrs+1] = z_arr
        return scall_fs

    def get_geolocation_full_size(self, pol, name):
        """ Get geolocation array with full resolution for the entire scene """
        i = self.geolocation_interpolator(pol, self.geolocation(pol)[name])
        rows = np.arange(self.shape(pol)[0])
        cols = np.arange(self.shape(pol)[1])
        return i(rows, cols)