#!/usr/bin/env python3

import argparse
import glob
import logging
import os
import pathlib
from multiprocessing import Pool

import numpy as np
import pandas as pd

from your.candidate import Candidate, crop
from your.utils import gpu_dedisp_and_dmt_crop

logger = logging.getLogger()


def normalise(data):
    """
    Noramlise the data by unit standard deviation and zero median
    :param data: data
    :return:
    """
    data = np.array(data, dtype=np.float32)
    data -= np.median(data)
    data /= np.std(data)
    return data


def cpu_dedisp_dmt(cand):
    pulse_width = cand.width
    if pulse_width == 1:
        time_decimate_factor = 1
    else:
        time_decimate_factor = pulse_width // 2
    logger.debug(f"Time decimation factor {time_decimate_factor}")

    cand.dmtime()
    logger.info('Made DMT')
    if args.opt_dm:
        logger.info('Optimising DM')
        logger.warning('This feature is experimental!')
        cand.optimize_dm()
    else:
        cand.dm_opt = -1
        cand.snr_opt = -1
    cand.dedisperse()
    logger.info('Made Dedispersed profile')

    # Frequency - Time reshaping
    cand.decimate(key='ft', axis=0, pad=True, decimate_factor=time_decimate_factor, mode='median')
    crop_start_sample_ft = cand.dedispersed.shape[0] // 2 - args.time_size // 2
    cand.dedispersed = crop(cand.dedispersed, crop_start_sample_ft, args.time_size, 0)
    logger.info(f'Decimated Time axis of FT to tsize: {cand.dedispersed.shape[0]}')
    # DM-time reshaping
    cand.decimate(key='dmt', axis=1, pad=True, decimate_factor=time_decimate_factor, mode='median')
    crop_start_sample_dmt = cand.dmt.shape[1] // 2 - args.time_size // 2
    cand.dmt = crop(cand.dmt, crop_start_sample_dmt, args.time_size, 1)
    logger.info(f'Decimated DM-Time to dmsize: {cand.dmt.shape[0]} and tsize: {cand.dmt.shape[1]}')
    return cand


def cand2h5(cand_val):
    """
    TODO: Add option to use cand.resize for reshaping FT and DMT
    Generates h5 file of candidate with resized frequency-time and DM-time arrays
    :param cand_val: List of candidate parameters (filename, snr, width, dm, label, tcand(s))
    :type cand_val: Candidate
    :return: None
    """
    filename, snr, width, dm, label, tcand, kill_mask_path, args = cand_val
    if kill_mask_path == kill_mask_path:
        kill_mask_file = pathlib.Path(kill_mask_path)
        if kill_mask_file.is_file():
            logger.info(f'Using mask {kill_mask_path}')
            kill_chans = np.loadtxt(kill_mask_path, dtype=np.int)
            filobj = SigprocFile(filename)
            kill_mask = np.zeros(filobj.nchans, dtype=np.bool)
            kill_mask[kill_chans] = True
    else:
        logger.debug('No Kill Mask')
        kill_mask = None

    fname, ext = os.path.splitext(filename)
    if ext == ".fits" or ext == ".sf":
        files = glob.glob(fname[:-5] + '*fits')
    elif ext == '.fil':
        files = [filename]
    else:
        raise TypeError("Can only work with list of fits file or filterbanks")

    logger.debug(f'Source file list: {files}')
    cand = Candidate(files, snr=snr, width=width, dm=dm, label=label, tcand=tcand, kill_mask=kill_mask,
                     device=args.gpu_id)
    cand.get_chunk()
    if cand.isfil:
        cand.fp.close()

    logger.info('Got Chunk')

    if args.gpu_id >= 0:
        logger.debug(f"Using the GPU {args.gpu_id}")
        try:
            cand = gpu_dedisp_and_dmt_crop(cand, device=args.gpu_id)
        except CudaAPIError:
            logger.info("Ran into a CudaAPIError, using the CPU version for this candidate")
            cand = cpu_dedisp_dmt(cand)
    else:
        cand = cpu_dedisp_dmt(cand)


    cand.resize(key='ft', size=args.frequency_size, axis=1, anti_aliasing=True)
    logger.info(f'Resized Frequency axis of FT to fsize: {cand.dedispersed.shape[1]}')
    cand.dmt = normalise(cand.dmt)
    cand.dedispersed = normalise(cand.dedispersed)
    fout = cand.save_h5(out_dir=args.fout)
    logger.info(fout)
    return None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Your candmaker! Make h5 candidates from the candidate csv files",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-v', '--verbose', help='Be verbose', action='store_true')
    parser.add_argument('-fs', '--frequency_size', type=int, help='Frequency size after rebinning', default=256)
    parser.add_argument('-g', '--gpu_id', help='GPU ID (use -1 for CPU)', type=int, required=False, default=-1)
    parser.add_argument('-ts', '--time_size', type=int, help='Time length after rebinning', default=256)
    parser.add_argument('-c', '--cand_param_file', help='csv file with candidate parameters', type=str, required=True)
    parser.add_argument('-n', '--nproc', type=int, help='number of processors to use in parallel (default: 2)',
                        default=2)
    parser.add_argument('-o', '--fout', help='Output file directory for candidate h5', type=str)
    parser.add_argument('-opt', '--opt_dm', dest='opt_dm', help='Optimise DM', action='store_true', default=False)
    values = parser.parse_args()

    logging_format = '%(asctime)s - %(funcName)s -%(name)s - %(levelname)s - %(message)s'

    if values.verbose:
        logging.basicConfig(level=logging.DEBUG, format=logging_format)
    else:
        logging.basicConfig(level=logging.INFO, format=logging_format)

    if values.gpu_id >= 0:
        from numba.cuda.cudadrv.driver import CudaAPIError
        logger.info(f"Using the GPU {values.gpu_id}")

    cand_pars = pd.read_csv(values.cand_param_file)
    process_list = []
    for index, row in cand_pars.iterrows():
        process_list.append(
            [row['file'], row['snr'], 2 ** row['width'], row['dm'], row['label'], row['stime'],
             row['kill_mask_path'], values])
    with Pool(processes=values.nproc) as pool:
        pool.map(cand2h5, process_list, chunksize=1)
