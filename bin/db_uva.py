#! /usr/bin/env python

"""
Use CNN activation to predict brain activation
"""

import os
import time
import argparse
import numpy as np
import pandas as pd
import pickle as pkl

from torch.utils.data import DataLoader
from torchvision import transforms
from os.path import join as pjoin
from dnnbrain.dnn.analyzer import dnn_activation, generate_bold_regressor
from dnnbrain.dnn.io import NetLoader, read_dnn_csv, PicDataset, VidDataset
from dnnbrain.brain.io import load_brainimg, save_brainimg
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.model_selection import cross_val_score
    

def main():
    parser = argparse.ArgumentParser(description='Use DNN activation to predict responses of brain or behavior '
                                                 'by univariate model.')
    parser.add_argument('-net',
                        type=str,
                        required=True,
                        metavar='NetName',
                        help='neural network name')
    parser.add_argument('-layer',
                        type=str,
                        required=True,
                        metavar='LayerName',
                        help="The name of the layer whose activation is used to predict brain activity. "
                             "For example, 'conv1' represents the first convolution layer, and "
                             "'fc1' represents the first full connection layer.")
    parser.add_argument('-dmask',
                        type=str,
                        required=False,
                        metavar='DnnMaskFile',
                        help='a db.csv file in which channles and columns of interest ae listed.')
    parser.add_argument('-iteraxis',
                        type=str,
                        metavar='Axis',
                        choices=['layer', 'channel', 'column'],
                        default='layer',
                        help="Iterate alone the specified axis."
                             "'channel': Summarize the maximal prediction score for each channel. "
                             "'column': Summarize the maximal prediction score for each channel. "
                             "Default is layer: Summarize the maximal prediction score for the whole layer.")
    parser.add_argument('-stim',
                        type=str,
                        required=True,
                        metavar='StimuliInfoFile',
                        help='a db.csv file which contains stimuli information')
    parser.add_argument('-hrf',
                        action='store_true',
                        help='Convolute dnn activation with SPM canonical hemodynamic response function. '
                             'And match it with the time points of Brain activation.')
    parser.add_argument('-resp',
                        type=str,
                        required=True,
                        metavar='Response',
                        help='A file contains responses used to be predicted by dnn activation. '
                             'If the file ends with .db.csv, usually means roi-wise or behavior data. '
                             'If the file is nifti/cifti file, usually means voxel/vertex-wise data.')
    parser.add_argument('-bmask',
                        type=str,
                        metavar='MaskFile',
                        help='Brain mask is used to extract activation locally. '
                             'Only used when the response file is nifti/cifti file.')
    parser.add_argument('-model',
                        type=str,
                        required=True,
                        metavar='BrainModel',
                        choices=['glm', 'lrc'],
                        help='Select a model to predict brain or behavior responses by dnn activation. '
                             'Use glm (general linear model) for regression analysis. '
                             'USe lrc (logistic regression) for classification analysis.')
    parser.add_argument('-cvfold',
                        type=int,
                        metavar='FoldNumber',
                        default=3,
                        help='cross validation fold number')
    parser.add_argument('-out',
                        type=str,
                        required=True,
                        metavar='OutputDir',
                        help='output directory. Output data will stored as the following directory: '
                             'layer/[channel/column]/max_score, max_position')
    args = parser.parse_args()

    # ---Extract DNN activation start---
    start1 = time.time()
    # Load DNN
    net_loader = NetLoader(args.net)
    transform = transforms.Compose([transforms.Resize(net_loader.img_size), transforms.ToTensor()])

    # Load stimuli
    stim_dict = read_dnn_csv(args.stim)
    if stim_dict['stimType'] == 'picture':
        dataset = PicDataset(stim_dict['stimPath'], stim_dict['variable'], transform=transform)
    elif stim_dict['stimType'] == 'video':
        dataset = VidDataset(stim_dict['stimPath'], stim_dict['variable'], transform=transform)
    else:
        raise TypeError('{} is not a supported stimulus type.'.format(stim_dict['stimType']))
    data_loader = DataLoader(dataset, batch_size=8, shuffle=False)

    # Extract activation
    print('Layer: {0}_{1}\nstimPath: {2}'.format(args.net, args.layer, stim_dict['stimPath']))
    if args.dmask is None:
        X = dnn_activation(data_loader, args.net, args.layer)
    else:
        dmask = read_dnn_csv(args.dmask)
        print('dmask: ', args.dmask)
        X = dnn_activation(data_loader, args.net, args.layer,
                           dmask['variable']['chn'], dmask['variable']['col'])
    n_stim, n_chn, n_col = X.shape
    end1 = time.time()
    print('Finish extracting dnn activaton: cost {} seconds'.format(end1 - start1))
    # ---Extract DNN activation end---

    # Load response
    if args.resp.endswith('.db.csv'):
        resp_dict = read_dnn_csv(args.resp)
        Y = np.array(list(resp_dict['variable'].values())).T

    elif args.resp.endswith('.nii') or args.resp.endswith('.nii.gz'):
        Y, header = load_brainimg(args.resp)
        bshape = Y.shape

        # Get resp data within brain mask
        if args.bmask is None:
            bmask = np.any(Y, 0)
        else:
            bmask, _ = load_brainimg(args.bmask, ismask=True)
            assert bshape[1:] == bmask.shape, 'brain mask and brain response mismatched in space'
            bmask = bmask.astype(np.bool)
        Y = Y[:, bmask]

    else:
        raise IOError('Only .db.csv and nifti/cifti are supported')
    n_samp, n_feat = Y.shape  # n_sample x n_feature
    print('Finish loading response: ', args.resp)

    # Convolute with HRF
    if args.hrf:
        start2 = time.time()
        onset = stim_dict['variable']['onset']
        duration = stim_dict['variable']['duration']
        tr = float(stim_dict['hrf_tr'])
        ops = int(stim_dict.get('hrf_ops', 100))
        X = generate_bold_regressor(X.reshape(n_stim, -1), onset, duration, n_samp, tr, ops)
        X = X.reshape(n_samp, n_chn, n_col)  # cover X before HRFed, save memory
        end2 = time.time()
        print('Finish HRF convolution: cost {} seconds'.format(end2 - start2))

    # ---Do prediction start---
    start3 = time.time()
    # Prepare model
    if args.model == 'glm':
        model = LinearRegression()
        score_evl = 'explained_variance'
    else:
        model = LogisticRegression()
        score_evl = 'accuracy'
    print('Prediction model:', args.model)

    # Perform univariate prediction analysis
    if args.iteraxis == 'channel':
        score_arr = np.zeros((n_chn, n_feat), dtype=np.float)
        position_arr = np.zeros_like(score_arr, dtype=np.int)
        model_arr = np.zeros_like(score_arr, dtype=np.object)
        for feat_idx in range(n_feat):
            for chn_idx in range(n_chn):
                score_tmp = []
                for col_idx in range(n_col):
                    cv_scores = cross_val_score(model, X[:, chn_idx, col_idx][:, np.newaxis],
                                                Y[:, feat_idx], scoring=score_evl, cv=args.cvfold)
                    score_tmp.append(np.mean(cv_scores))
                max_score = max(score_tmp)
                max_col_idx = score_tmp.index(max_score)
                score_arr[chn_idx, feat_idx] = max_score
                position = max_col_idx+1 if args.dmask is None else dmask['variable']['col'][max_col_idx]+1
                position_arr[chn_idx, feat_idx] = position
                model_arr[chn_idx, feat_idx] = model.fit(X[:, chn_idx, max_col_idx][:, np.newaxis], Y[:, feat_idx])
                print('Feat_idx{}: finish iterate chn_idx{}'.format(feat_idx, chn_idx))
    elif args.iteraxis == 'column':
        score_arr = np.zeros((n_col, n_feat), dtype=np.float)
        position_arr = np.zeros_like(score_arr, dtype=np.int)
        model_arr = np.zeros_like(score_arr, dtype=np.object)
        for feat_idx in range(n_feat):
            for col_idx in range(n_col):
                score_tmp = []
                for chn_idx in range(n_chn):
                    cv_scores = cross_val_score(model, X[:, chn_idx, col_idx][:, np.newaxis],
                                                Y[:, feat_idx], scoring=score_evl, cv=args.cvfold)
                    score_tmp.append(np.mean(cv_scores))
                max_score = max(score_tmp)
                max_chn_idx = score_tmp.index(max_score)
                score_arr[col_idx, feat_idx] = max_score
                position = max_chn_idx+1 if args.dmask is None else dmask['variable']['chn'][max_chn_idx]+1
                position_arr[col_idx, feat_idx] = position
                model_arr[col_idx, feat_idx] = model.fit(X[:, max_chn_idx, col_idx][:, np.newaxis], Y[:, feat_idx])
                print('Feat_idx{}: finish iterate col_idx{}'.format(feat_idx, col_idx))
    else:
        score_arr = np.zeros((1, n_feat), dtype=np.float)
        position_arr = np.zeros((2, n_feat), dtype=np.int)
        model_arr = np.zeros_like(score_arr, dtype=np.object)
        for feat_idx in range(n_feat):
            score_tmp = np.zeros((n_chn, n_col))
            for chn_idx in range(n_chn):
                for col_idx in range(n_col):
                    cv_scores = cross_val_score(model, X[:, chn_idx, col_idx][:, np.newaxis],
                                                Y[:, feat_idx], scoring=score_evl, cv=args.cvfold)
                    score_tmp[chn_idx, col_idx] = np.mean(cv_scores)
            max_score = np.max(score_tmp)
            max_indices = np.where(score_tmp == max_score)
            max_chn_idx = max_indices[0][0]
            max_col_idx = max_indices[1][0]
            score_arr[0, feat_idx] = max_score
            if args.dmask is None:
                pos_chn = max_chn_idx + 1
                pos_col = max_col_idx + 1
            else:
                pos_chn = dmask['variable']['chn'][max_chn_idx] + 1
                pos_col = dmask['variable']['col'][max_col_idx] + 1
            position_arr[0, feat_idx] = pos_chn
            position_arr[1, feat_idx] = pos_col
            model_arr[0, feat_idx] = model.fit(X[:, max_chn_idx, max_col_idx][:, np.newaxis], Y[:, feat_idx])
            print('Feat_idx{}: finished'.format(feat_idx))
    end3 = time.time()
    print('Finish prediction: cost {} seconds'.format(end3 - start3))
    # ---Do prediction end---

    # Save out
    resp_suffix = '.'.join(args.resp.split('.')[1:])
    layer_dir = pjoin(args.out, '{0}_{1}'.format(args.net, args.layer))
    if not os.path.isdir(layer_dir):
        os.makedirs(layer_dir)
    if args.iteraxis == 'channel':
        channel_dir = pjoin(layer_dir, 'channel')
        if not os.path.isdir(channel_dir):
            os.makedirs(channel_dir)
        if args.resp.endswith('.db.csv'):
            score_df = pd.DataFrame(score_arr, columns=resp_dict['variable'].keys())
            del score_arr  # save memory
            position_df = pd.DataFrame(position_arr, columns=resp_dict['variable'].keys())
            del position_arr  # save memory
            score_df.to_csv(pjoin(channel_dir, 'max_score.csv'), index=False)
            if args.dmask is None:
                chn_nums = range(1, n_chn+1)
            else:
                chn_nums = [i + 1 for i in dmask['variable']['chn']]
            position_df.insert(0, 'chn_num', chn_nums)
            position_df.to_csv(pjoin(channel_dir, 'max_position.csv'), index=False)
        elif args.resp.endswith('.nii') or args.resp.endswith('.nii.gz'):
            score_img = np.zeros((n_chn, *bshape[1:]))
            score_img[:, bmask] = score_arr
            del score_arr  # save memory
            position_img = np.zeros_like(score_img)
            position_img[:, bmask] = position_arr
            del position_arr  # save memory
            save_brainimg(pjoin(channel_dir, 'max_score.'+resp_suffix), score_img, header)
            save_brainimg(pjoin(channel_dir, 'max_position.'+resp_suffix), position_img, header)
        pkl.dump(model_arr, open(pjoin(channel_dir, 'max_model.pkl'), 'wb'))

    elif args.iteraxis == 'column':
        column_dir = pjoin(layer_dir, 'column')
        if not os.path.isdir(column_dir):
            os.makedirs(column_dir)
        if args.resp.endswith('.db.csv'):
            score_df = pd.DataFrame(score_arr, columns=resp_dict['variable'].keys())
            del score_arr  # save memory
            position_df = pd.DataFrame(position_arr, columns=resp_dict['variable'].keys())
            del position_arr  # save memory
            score_df.to_csv(pjoin(column_dir, 'max_score.csv'), index=False)
            if args.dmask is None:
                col_nums = range(1, n_col+1)
            else:
                col_nums = [i + 1 for i in dmask['variable']['col']]
            position_df.insert(0, 'col_num', col_nums)
            position_df.to_csv(pjoin(column_dir, 'max_position.csv'), index=False)
        elif args.resp.endswith('.nii') or args.resp.endswith('.nii.gz'):
            score_img = np.zeros((n_col, *bshape[1:]))
            score_img[:, bmask] = score_arr
            del score_arr  # save memory
            position_img = np.zeros_like(score_img)
            position_img[:, bmask] = position_arr
            del position_arr  # save memory
            save_brainimg(pjoin(column_dir, 'max_score.' + resp_suffix), score_img, header)
            save_brainimg(pjoin(column_dir, 'max_position.' + resp_suffix), position_img, header)
        pkl.dump(model_arr, open(pjoin(column_dir, 'max_model.pkl'), 'wb'))

    else:
        if args.resp.endswith('.db.csv'):
            score_df = pd.DataFrame(score_arr, columns=resp_dict['variable'].keys())
            del score_arr  # save memory
            position_df = pd.DataFrame(position_arr, columns=resp_dict['variable'].keys())
            del position_arr  # save memory
            score_df.to_csv(pjoin(layer_dir, 'max_score.csv'), index=False)
            position_df.insert(0, 'axis', ['channel', 'column'])
            position_df.to_csv(pjoin(layer_dir, 'max_position.csv'), index=False)
        elif args.resp.endswith('.nii') or args.resp.endswith('.nii.gz'):
            score_img = np.zeros((1, *bshape[1:]))
            score_img[0, bmask] = score_arr
            del score_arr  # save memory
            position_img = np.zeros((2, *bshape[1:]))
            position_img[:, bmask] = position_arr
            del position_arr  # save memory
            save_brainimg(pjoin(layer_dir, 'max_score.' + resp_suffix), score_img, header)
            save_brainimg(pjoin(layer_dir, 'max_position.' + resp_suffix), position_img, header)
        pkl.dump(model_arr, open(pjoin(layer_dir, 'max_model.pkl'), 'wb'))


if __name__ == '__main__':
    main()
