"""
"""

from abc import ABC, abstractmethod
from numbers import Real
from typing import Optional, List

import numpy as np
np.set_printoptions(precision=5, suppress=True)
from scipy.ndimage.filters import median_filter
from scipy.signal.signaltools import resample
# from scipy.signal import medfilt
# https://github.com/scipy/scipy/issues/9680
from biosppy.signals.tools import filter_signal

import numpy as np


__all__ = [
    "PreProcessor",
    "preprocess_multi_lead_signal",
    "preprocess_single_lead_signal",
]


class PreProcessor(ABC):
    """
    a preprocessor do preprocessing for ECGs
    """
    __name__ = "PreProcessor"

    @abstractmethod
    def apply(self, sig:np.ndarray, fs:Real) -> np.ndarray:
        """
        apply the preprocessor to `sig`

        Parameters
        ----------
        sig: ndarray,
            the ECG signal, can be
            1d array, which is a single-lead ECG
            2d array, which is a multi-lead ECG of "lead_first" format
            3d array, which is a tensor of several ECGs, of shape (batch, lead, siglen)
        fs: real number,
            sampling frequency of the ECG signal
        """
        raise NotImplementedError

    def __call__(self, sig:np.ndarray, fs:Real) -> np.ndarray:
        """
        alias of `self.apply`
        """
        return self.apply(sig)


def preprocess_multi_lead_signal(raw_sig:np.ndarray,
                                 fs:Real,
                                 sig_fmt:str="channel_first",
                                 bl_win:Optional[List[Real]]=None,
                                 band_fs:Optional[List[Real]]=None,) -> np.ndarray:
    """ finished, checked,

    perform preprocessing for multi-lead ecg signal (with units in mV),
    preprocessing may include median filter, bandpass filter, and rpeaks detection, etc.

    Parameters
    ----------
    raw_sig: ndarray,
        the raw ecg signal, with units in mV
    fs: real number,
        sampling frequency of `raw_sig`
    sig_fmt: str, default "channel_first",
        format of the multi-lead ecg signal,
        "channel_last" (alias "lead_last"), or
        "channel_first" (alias "lead_first", original)
    bl_win: list (of 2 real numbers), optional,
        window (units in second) of baseline removal using `median_filter`,
        the first is the shorter one, the second the longer one,
        a typical pair is [0.2, 0.6],
        if is None or empty, baseline removal will not be performed
    band_fs: list (of 2 real numbers), optional,
        frequency band of the bandpass filter,
        a typical pair is [0.5, 45],
        be careful when detecting paced rhythm,
        if is None or empty, bandpass filtering will not be performed
    verbose: int, default 0,
        print verbosity

    Returns
    -------
    filtered_ecg: ndarray,
        the array of the processed ecg signal,
        the format of the signal is kept the same with the original signal, i.e. `sig_fmt`
    """
    assert sig_fmt.lower() in ["channel_first", "lead_first", "channel_last", "lead_last"]
    if sig_fmt.lower() in ["channel_last", "lead_last"]:
        filtered_ecg = raw_sig.T
    else:
        filtered_ecg = raw_sig.copy()
    cpu_num = max(1, mp.cpu_count()-3)
    with mp.Pool(processes=cpu_num) as pool:
        results = pool.starmap(
            func=preprocess_single_lead_signal,
            iterable=[(filtered_ecg[lead,...], fs, bl_win, band_fs, verbose) for lead in range(filtered_ecg.shape[0])]
        )
    for lead in range(filtered_ecg.shape[0]):
        filtered_ecg[lead,...] = results[lead]
    if sig_fmt.lower() in ["channel_last", "lead_last"]:
        filtered_ecg = filtered_ecg.T

    return filtered_ecg


def preprocess_single_lead_signal(raw_sig:np.ndarray,
                                  fs:Real,
                                  bl_win:Optional[List[Real]]=None,
                                  band_fs:Optional[List[Real]]=None,
                                  verbose:int=0,) -> np.ndarray:
    """ finished, checked,

    perform preprocessing for single lead ecg signal (with units in mV),
    preprocessing may include median filter, bandpass filter, and rpeaks detection, etc.

    Parameters
    ----------
    raw_sig: ndarray,
        the raw ecg signal, with units in mV
    fs: real number,
        sampling frequency of `raw_sig`
    bl_win: list (of 2 real numbers), optional,
        window (units in second) of baseline removal using `median_filter`,
        the first is the shorter one, the second the longer one,
        a typical pair is [0.2, 0.6],
        if is None or empty, baseline removal will not be performed
    band_fs: list (of 2 real numbers), optional,
        frequency band of the bandpass filter,
        a typical pair is [0.5, 45],
        be careful when detecting paced rhythm,
        if is None or empty, bandpass filtering will not be performed
    verbose: int, default 0,
        print verbosity

    Returns
    -------
    filtered_ecg: ndarray,
        the array of the processed ecg signal

    NOTE
    ----
    bandpass filter uses FIR filters, an alternative can be Butterworth filter,
    e.g. `butter_bandpass_filter` in `utils.utils_singal`
    """
    filtered_ecg = raw_sig.copy()

    # remove baseline
    if bl_win:
        window1 = 2 * (int(bl_win[0] * fs) // 2) + 1  # window size must be odd
        window2 = 2 * (int(bl_win[1] * fs) // 2) + 1
        baseline = median_filter(filtered_ecg, size=window1, mode="nearest")
        baseline = median_filter(baseline, size=window2, mode="nearest")
        filtered_ecg = filtered_ecg - baseline
    
    # filter signal
    if band_fs:
        assert band_fs[0] < band_fs[1]
        nyq = 0.5 * fs
        if band_fs[0] <= 0 and band_fs[1] < nyq:
            band = "lowpass"
            frequency = band_fs[1]
        elif band_fs[1] >= nyq and band_fs[0] > 0:
            band = "highpass"
            frequency = band_fs[0]
        elif band_fs[0] > 0 and band_fs[1] < nyq:
            band = "bandpass"
            frequency = band_fs
        else:
            raise ValueError("Invalid frequency band")
        filtered_ecg = filter_signal(
            signal=filtered_ecg,
            ftype="FIR",
            # ftype="butter",
            band=band,
            order=int(0.3 * fs),
            sampling_rate=fs,
            frequency=frequency,
        )["signal"]
    
    return filtered_ecg
