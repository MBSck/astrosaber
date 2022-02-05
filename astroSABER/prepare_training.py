import numpy as np
import pickle
from astropy.io import fits
from astropy import units as u
from scipy import sparse
from scipy.sparse.linalg import spsolve

from tqdm import tqdm, trange
import warnings
import os

from .utils.quality_checks import goodness_of_fit, get_max_consecutive_channels, determine_peaks, mask_channels
from .utils.aslsq_helper import velocity_axes, count_ones_in_row, md_header_2d, check_signal_ranges, IterationWarning, say, format_warning
from .utils.aslsq_fit import baseline_als_optimized
from .plotting import plot_pickle_spectra

warnings.showwarning = format_warning
np.seterr('raise')



class saberPrepare(object):
    def __init__(self, fitsfile, training_set_size=100, path_to_noise_map=None, path_to_data='.', mean_linewidth=4.,std_linewidth=1., lam1=None, p1=None, lam2=None, p2=None, niters=50, iterations_for_convergence=3, noise=None, add_residual = False, sig = 1.0, velo_range = 15.0, check_signal_sigma = 6., p_limit=None, ncpus=1, suffix='', filename_out=None, path_to_file='.', seed=111):
        self.fitsfile = fitsfile
        self.training_set_size = int(training_set_size)
        self.path_to_noise_map = path_to_noise_map
        self.path_to_data = path_to_data

        self.mean_linewidth = mean_linewidth
        self.std_linewidth = std_linewidth
        
        self.lam1 = lam1
        self.p1 = p1
        self.lam2 = lam2
        self.p2 = p2
        
        self.niters = int(niters)
        self.iterations_for_convergence = int(iterations_for_convergence)
        
        self.noise = noise
        self.add_residual = add_residual
        self.sig = sig
        
        self.velo_range = velo_range
        self.check_signal_sigma = check_signal_sigma

        self.p_limit = p_limit

        self.ncpus = ncpus

        self.suffix = suffix
        self.filename_out = filename_out
        self.path_to_file = path_to_file
        
        self.seed = seed
        
        self.debug_data = None # for debugging
        
    def __str__(self):
        return f'saberPrepare:\nfitsfile: {self.fitsfile}\ntraining_set_size: {self.training_set_size}\npath_to_noise_map: {self.path_to_noise_map}\npath_to_data: {self.path_to_data}\nmean_linewidth: {self.mean_linewidth}\nstd_linewidth: {self.std_linewidth}\nlam1: {self.lam1}\np1: {self.p1}\nlam2: {self.lam2}\np2: {self.p2}\nniters: {self.niters}\niterations_for_convergence: {self.iterations_for_convergence}\nnoise: {self.noise}\nadd_residual: {self.add_residual}\nsig: {self.sig}\nvelo_range: {self.velo_range}\ncheck_signal_sigma: {self.check_signal_sigma}\np_limit: {self.p_limit}\nncpus: {self.ncpus}\nsuffix: {self.suffix}\nfilename_out: {self.filename_out}\nseed: {self.seed}'
    
    def getting_ready(self):
        string = 'preparation'
        banner = len(string) * '='
        heading = '\n' + banner + '\n' + string + '\n' + banner
        say(heading)

    def prepare_data(self):
        self.getting_ready()
        self.image = fits.getdata(self.fitsfile) #load data
        self.image[np.where(np.isnan(self.image))] = 0.0

        self.header = fits.getheader(self.fitsfile)
        #self.header_2d = md_header_2d(self.fitsfile)
        self.v = self.header['NAXIS3']
        self.velocity = velocity_axes(self.fitsfile)
        self.mock_data = {'training_data' : None, 'test_data' : None, 'hisa_spectra' : None, 'hisa_mask' : None, 'signal_ranges' : None, 'rms_noise' : None, 'velocity' : None, 'header' : None}
        self.hisa_spectra = []
        self.training_data = []
        self.test_data = []
        self.hisa_mask = []
        self.signal_ranges = []
        string = 'Done!'
        say(string)

    def prepare_training(self):
        self.rng = np.random.default_rng(self.seed)
        self.prepare_data()

        if self.training_set_size <= 0:
            raise ValueError("'training_set_size' has to be >0")
        if self.lam1 is None:
            self.lam1 = 2.00
        if self.p1 is None:
            self.p1 = 0.90
        if not 0<= self.p1 <=1:
            raise ValueError("'p1' has to be in the range [0,1]")
        if self.lam2 is None:
            self.lam2 = 2.00
        if self.p2 is None:
            self.p2 = 0.90
        if not 0<= self.p2 <=1:
            raise ValueError("'p2' has to be in the range [0,1]")

        if self.path_to_noise_map is not None:
            noise_map = fits.getdata(self.path_to_noise_map)
            thresh = self.sig * noise_map
        else:
            if self.noise is None:
               raise TypeError("Need to specify 'noise' if no path to noise map is given.") 
            else:
                noise_map = self.noise * np.ones((self.header['NAXIS2'],self.header['NAXIS1']))
                thresh = self.sig * noise_map

        if self.p_limit is None:
            self.p_limit = 0.02
            
        string = 'creating training data'
        banner = len(string) * '='
        heading = '\n' + banner + '\n' + string + '\n' + banner
        say(heading)

        self.max_consec_ch = get_max_consecutive_channels(self.v, self.p_limit)
        channel_width = self.header['CDELT3'] / 1000.
        spectral_resolution = 1 / np.sqrt(8*np.log(2)) # unit channel
        edges = int(0.10 * min(self.header['NAXIS1'],self.header['NAXIS2']))
        indices = np.column_stack((self.rng.integers(edges,self.header['NAXIS2']-edges+1,self.training_set_size), self.rng.integers(edges,self.header['NAXIS1']-edges+1,self.training_set_size)))

        mu_lws_HISA, sigma_lws_HISA = (self.mean_linewidth / channel_width) / np.sqrt(8*np.log(2)), self.std_linewidth / channel_width # mean and standard deviation
        mu_ncomps_HISA, sigma_ncomps_HISA = 2, 1 
        ncomps_HISA = np.around(self.rng.normal(mu_ncomps_HISA, sigma_ncomps_HISA, self.training_set_size).reshape(self.training_set_size)).astype(int)
        ###TODO
        ncomps_HISA[ncomps_HISA<=0] = int(1.)

        xvals = np.arange(0,self.v,1)
        
        self.spectrum_list = []
        self.noise_list = []
        self.thresh_list = []
        for idx, (y, x) in enumerate(zip(indices[:,0], indices[:,1])):
            self.spectrum_list.append(self.image[:,y,x])
            self.noise_list.append(noise_map[y,x])
            self.thresh_list.append(thresh[y,x])
        import astroSABER.parallel_processing
        astroSABER.parallel_processing.init([self.spectrum_list, [self]])
        #ilist = np.arange(len(self.spectrum_list))
        results_list = astroSABER.parallel_processing.func(use_ncpus=self.ncpus, function='hisa') # initiate parallel process

        for i in trange(len(results_list)):
            #Check for NaNs in the test spectra
            if np.any(np.isnan(results_list[i][0])):
                print('Mock spectrum contains NaN! Will remove it!')
                continue
            samplesize_rng = 10 * ncomps_HISA[i]
            amps_HISA = self.rng.normal(results_list[i][3], results_list[i][4], samplesize_rng).reshape(samplesize_rng,) # self.training_set_size
            amps_HISA[amps_HISA<0] = 0.
            ###TODO
            velos_of_comps_HISA = []
            for _ in range(ncomps_HISA[i]):
                k = 0
                mu_velos_HISA_k, sigma_velos_HISA_k = (results_list[i][1][k,0] + results_list[i][1][k,1]) / 2., (results_list[i][1][k,1] - results_list[i][1][k,0]) / (np.sqrt(8*np.log(2))) / 3. # mean and standard deviation
                if k < len(results_list[i][1][:,0])-1:
                    k += 1
                velos_HISA_k = self.rng.normal(mu_velos_HISA_k, sigma_velos_HISA_k, samplesize_rng).reshape(samplesize_rng,)
                velos_of_comps_HISA_k = self.rng.choice(velos_HISA_k, 1)
                if not velos_of_comps_HISA_k < 0. or velos_of_comps_HISA_k > self.v:
                    velos_of_comps_HISA.append(velos_of_comps_HISA_k)
            velos_of_comps_HISA = np.array(velos_of_comps_HISA) 
            lws_HISA = self.rng.normal(mu_lws_HISA, sigma_lws_HISA, samplesize_rng).reshape(samplesize_rng,) # 
            amps_of_comps_HISA = self.rng.choice(amps_HISA, ncomps_HISA[i])
            lws_of_comps_HISA = self.rng.choice(lws_HISA, ncomps_HISA[i])  
            ncomp_HISA = np.arange(0,ncomps_HISA[i]+1,1)
            lws_of_comps_HISA[np.where(lws_of_comps_HISA<3*spectral_resolution)] = 3*spectral_resolution

            gauss_HISA = np.zeros(shape=(self.v,))
            ranges_hisa_list = []
            for idx, (v, lw, amp) in enumerate(zip(velos_of_comps_HISA,lws_of_comps_HISA,amps_of_comps_HISA)):
                exp_arg = 0.5 * ((xvals - v) / lw)**2
                exp_arg[np.where(exp_arg>100.)] = 100.
                #limit HISA to HI emission
                if amp>results_list[i][0][int(np.around(v))]:
                    amp = results_list[i][0][int(np.around(v))]
                gauss_HISA = gauss_HISA + amp * np.exp(-exp_arg)
                ranges_hisa_i = [np.around(v - 3*lw), np.around(v + 3*lw)]
                ranges_hisa_list.append(ranges_hisa_i)
            gauss_HISA[np.where(gauss_HISA<1e-5)] = 0.
                
            ranges_hisa = np.array(ranges_hisa_list).astype(int).reshape(-1,2)
            sort_indices = np.argsort(ranges_hisa[:, 0])
            ranges_hisa = ranges_hisa[sort_indices]
            consecutive_channels_hisa = ranges_hisa[:, 1] - ranges_hisa[:, 0]
            mask_ranges_hisa = ranges_hisa[np.where(consecutive_channels_hisa>=0)]
            pad = 3
            for j in range(mask_ranges_hisa.shape[0]):
                lower = max(0, mask_ranges_hisa[j,0] - pad)
                upper = min(self.v, mask_ranges_hisa[j,1] + pad)
                mask_ranges_hisa[j,0], mask_ranges_hisa[j,1] = lower, upper
            mask_hisa = mask_channels(self.v, mask_ranges_hisa, pad_channels=pad, remove_intervals=None)
            #mask HISA where no HI emission
            gauss_HISA[np.invert(results_list[i][2])] = 0.   

            self.training_data.append(results_list[i][0] - gauss_HISA)
            self.test_data.append(results_list[i][0])
            self.hisa_spectra.append(gauss_HISA)
            self.hisa_mask.append(mask_hisa)
            self.signal_ranges.append(mask_ranges_hisa)

        self.mock_data['training_data'] = self.training_data
        self.mock_data['test_data'] = self.test_data
        self.mock_data['hisa_spectra'] = self.hisa_spectra
        self.mock_data['hisa_mask'] = self.hisa_mask
        self.mock_data['signal_ranges'] = self.signal_ranges
        self.mock_data['rms_noise'] = self.noise_list
        self.mock_data['velocity'] = self.velocity
        self.mock_data['header'] = self.header

        self.save_data()
        plot_pickle_spectra(self.path_to_file, outfile=None, ranges=None, path_to_plots='astrosaber_training/plots', n_spectra=20, rowsize=4., rowbreak=10, dpi=72, velocity_range=[-110,163], vel_unit=u.km/u.s, seed=self.seed)

    def two_step_extraction(self, i):
        flag = 1.
        if check_signal_ranges(self.spectrum_list[i], self.header, sigma=self.check_signal_sigma, noise=self.noise_list[i], velo_range=self.velo_range):
            spectrum_prior = baseline_als_optimized(self.spectrum_list[i], self.lam1, self.p1, niter=3)
            spectrum_firstfit = spectrum_prior
            converge_logic = np.array([])
            for n in range(niters+1):
                spectrum_prior = baseline_als_optimized(spectrum_prior, self.lam2, self.p2, niter=3)
                spectrum_next = baseline_als_optimized(spectrum_prior, self.lam2, self.p2, niter=3)
                residual = abs(spectrum_next - spectrum_prior)
                if np.any(np.isnan(residual)):
                    print('Residual contains NaNs') 
                    residual[np.isnan(residual)] = 0.0
                converge_test = (np.all(residual < self.thresh_list[i]))
                converge_logic = np.append(converge_logic,converge_test)
                c = count_ones_in_row(converge_logic)
                if np.any(c > self.iterations_for_convergence):
                    i_converge = np.min(np.argwhere(c > self.iterations_for_convergence))
                    res = abs(spectrum_next - spectrum_firstfit)
                    if self.add_residual:
                        final_spec = spectrum_next + res
                    else:
                        final_spec = spectrum_next
                    break
                elif n==self.niters:
                    warnings.warn('Maximum number of iterations reached. Fit did not converge.', IterationWarning)
                    #flags
                    flag = 0.
                    res = abs(spectrum_next - spectrum_firstfit)
                    if self.add_residual:
                        final_spec = spectrum_next + res
                    else:
                        final_spec = spectrum_next
                    i_converge = self.niters
                    break
            bg = final_spec - self.thresh_list[i]
            #TODO
            offset_bg = np.nanmean([bg[0], bg[-1]])
            bg = bg - offset_bg
            #
            hisa = final_spec - self.spectrum_list[i] - self.thresh_list[i]
            iterations = i_converge
        else:
            bg = np.nan
            hisa = np.nan
            iterations = np.nan
            #flags
            flag = 0.

        consecutive_channels, ranges = determine_peaks(spectrum=self.spectrum_list[i], peak='positive', amp_threshold=None)
        #amp_values, ranges = determine_peaks(spectrum=data[:,y,x], peak='positive', amp_threshold=6*self.noise_list[i])
        mask_ranges = ranges[np.where(consecutive_channels>=self.max_consec_ch)]
        mask = mask_channels(self.v, mask_ranges, pad_channels=-5, remove_intervals=None)
        
        obs_noise = self.rng.normal(0,self.noise_list[i],size=(self.v,))
        mock_emission = bg + obs_noise

        mu_amps_HISA, sigma_amps_HISA = 6*self.noise_list[i], 1*self.noise_list[i]

        return mock_emission, mask_ranges, mask, mu_amps_HISA, sigma_amps_HISA

    def save_data(self):
        if self.filename_out is None:
            filename_out = '{}-training_set-{}_spectra{}.pickle'.format(self.fitsfile.split('/')[-1].split('.fits')[0], self.training_set_size, self.suffix)
        elif not self.filename_out.endswith('.pickle'):
            filename_out = self.filename_out + '.pickle'
        dirname = os.path.join(self.path_to_data, 'astrosaber_training')
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        self.path_to_file = os.path.join(dirname, filename_out)
        pickle.dump(self.mock_data, open(self.path_to_file, 'wb'), protocol=2)
        say("\n\033[92mSAVED FILE:\033[0m '{}' in '{}'".format(filename_out, dirname))