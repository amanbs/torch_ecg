"""
References:
-----------
[1] http://2019.icbeb.org/Challenge.html
"""
import math
from typing import Union, Optional, Sequence
from numbers import Real

import numpy as np


__all__ = [
    "compute_metrics",
]


def compute_metrics(rpeaks_truth:Sequence[Union[np.ndarray,Sequence[int]]], rpeaks_pred:Sequence[Union[np.ndarray,Sequence[int]]], fs:Real, thr:float=0.075, verbose:int=0) -> float:
    """ finished, NOT checked,

    Parameters:
    -----------
    rpeaks_truth: sequence,
        sequence of ground truths of rpeaks locations from multiple records
    rpeaks_pred: sequence,
        predictions of ground truths of rpeaks locations for multiple records
    fs: real number,
        sampling frequency of ECG signal
    thr: float, default 0.075,
        threshold for a prediction to be truth positive,
        with units in seconds,
    verbose: int, default 0,
        print verbosity

    Returns:
    --------
    rec_acc: float,
        accuracy of predictions
    """
    assert len(rpeaks_truth) == len(rpeaks_pred), \
        f"number of records does not match, truth indicates {len(rpeaks_truth)}, while pred indicates {len(rpeaks_pred)}"
    n_records = len(rpeaks_truth)
    record_flags = np.ones((len(rpeaks_truth),))
    thr_ = thr * fs
    if verbose >= 1:
        print(f"number of records = {n_records}")
        print(f"threshold in number of sample points = {thr_}")
    for idx, (truth_arr, pred_arr) in enumerate(zip(rpeaks_truth, rpeaks_pred)):
        false_negative = 0
        false_positive = 0
        true_positive = 0
        for j in range(len(truth_arr)):
            loc = np.where(np.abs(pred_arr - truth_arr[j]) <= thr_)[0]
            if j == 0:
                err = np.where((pred_arr >= 0.5*fs + thr_) & (pred_arr <= truth_arr[j] - thr_))[0]
            elif j == len(truth_arr)-1:
                err = np.where((pred_arr >= truth_arr[j]+thr_) & (pred_arr <= 9.5*fs - thr_))[0]
            else:
                err = np.where((pred_arr >= truth_arr[j]+thr_) & (pred_arr <= truth_arr[j+1]-thr_))[0]

            false_positive += len(err)
            if len(loc) >= 1:
                true_positive += 1
                false_positive += len(loc) - 1
            elif len(loc) == 0:
                false_negative += 1

        if false_negative + false_positive > 1:
            record_flags[idx] = 0
        elif false_negative == 1 and false_positive == 0:
            record_flags[idx] = 0.3
        elif false_negative == 0 and false_positive == 1:
            record_flags[idx] = 0.7

        if verbose >= 2:
            print(f"for the {idx}-th record,\ntrue positive = {true_positive}\nfalse positive = {false_positive}\nfalse negative = {false_negative}")

    rec_acc = round(np.sum(record_flags) / n_records, 4)
    print('QRS_acc: {}'.format(rec_acc))
    print('Scoring complete.')

    return rec_acc


def score(r_ref, hr_ref, r_ans, hr_ans, fs_, thr_):
    """
    the official scoring function
    """
    HR_score = 0
    record_flags = np.ones(len(r_ref))
    for i in range(len(r_ref)):
        FN = 0
        FP = 0
        TP = 0

        if math.isnan(hr_ans[i]):
            hr_ans[i] = 0
        hr_der = abs(int(hr_ans[i]) - int(hr_ref[i]))
        if hr_der <= 0.02 * hr_ref[i]:
            HR_score = HR_score + 1
        elif hr_der <= 0.05 * hr_ref[i]:
            HR_score = HR_score + 0.75
        elif hr_der <= 0.1 * hr_ref[i]:
            HR_score = HR_score + 0.5
        elif hr_der <= 0.2 * hr_ref[i]:
            HR_score = HR_score + 0.25

        for j in range(len(r_ref[i])):
            loc = np.where(np.abs(r_ans[i] - r_ref[i][j]) <= thr_*fs_)[0]
            if j == 0:
                err = np.where((r_ans[i] >= 0.5*fs_ + thr_*fs_) & (r_ans[i] <= r_ref[i][j] - thr_*fs_))[0]
            elif j == len(r_ref[i])-1:
                err = np.where((r_ans[i] >= r_ref[i][j]+thr_*fs_) & (r_ans[i] <= 9.5*fs_ - thr_*fs_))[0]
            else:
                err = np.where((r_ans[i] >= r_ref[i][j]+thr_*fs_) & (r_ans[i] <= r_ref[i][j+1]-thr_*fs_))[0]

            FP = FP + len(err)
            if len(loc) >= 1:
                TP += 1
                FP = FP + len(loc) - 1
            elif len(loc) == 0:
                FN += 1

        if FN + FP > 1:
            record_flags[i] = 0
        elif FN == 1 and FP == 0:
            record_flags[i] = 0.3
        elif FN == 0 and FP == 1:
            record_flags[i] = 0.7

    rec_acc = round(np.sum(record_flags) / len(r_ref), 4)
    hr_acc = round(HR_score / len(r_ref), 4)

    print( 'QRS_acc: {}'.format(rec_acc))
    print('HR_acc: {}'.format(hr_acc))
    print('Scoring complete.')

    return rec_acc, hr_acc