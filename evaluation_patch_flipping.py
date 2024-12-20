
import os
import json
import argparse

import pandas as pd
import torch

from training.callback import Callback
from models.model_factory import ModelFactory, xModelFactory
from datasets.dataset_factory import DatasetFactory
from xai.evaluation import xMILEval


def get_args():
    parser = argparse.ArgumentParser()

    # Loading and saving
    parser.add_argument('--model-path', type=str, required=True)
    parser.add_argument('--results-dir', type=str, required=True)
    parser.add_argument('--sel-checkpoint', type=str, default='best')
    parser.add_argument('--dataset', type=str, default='test',
                        help='the dataset for which the patch dropping is performed. can be train, val, or test.')
    parser.add_argument('--explanation-types', type=str, nargs='+', required=True)
    parser.add_argument('--explain-scores-path', type=str, default=None,
                        help='the path to the csv file containing the patch scores.')
    parser.add_argument('--precomputed-heatmap-types', default=None, nargs='+', type=str,
                        help='list of the heatmap types that have the precomputed patch scores')

    parser.add_argument('--max-bag-size', type=int, default=-1)
    parser.add_argument('--strategy', type=str, default='1%-of-all')
    parser.add_argument('--approach', type=str, default='drop', choices=['drop', 'add'])

    # Analyses
    parser.add_argument('--flipping', action='store_true',
                        help="Whether to perform patch dropping/adding")
    parser.add_argument('--baseline', action='store_true',
                        help="Whether to compute the random baseline")
    parser.add_argument('--morl-abs', action='store_true',
                        help="morl-abs:= most relevant last applied on absolute patch scores")

    # Explanations
    parser.add_argument('--explained-rel', type=str, default='logit',
                        help='The type of output to be explained.')
    parser.add_argument('--lrp-params', type=json.loads, default=None,
                        help='LRP params for LRP explanations.')
    parser.add_argument('--contrastive-class', type=int, default=None,
                        help='The class to be explained against (if explained-rel is contrastive).')
    parser.add_argument('--attention-layer', type=int, default=None,
                        help='For which attention layer to extract attention scores. If None, attention rollout '
                             'over all layers.')
    parser.add_argument('--detach-pe', action='store_true')
    parser.add_argument('---preload-data', action='store_true')

    parser.add_argument('--device', type=str, default='cpu')

    args = parser.parse_args()

    # convert -1 to None
    args.contrastive_class = None if args.contrastive_class == -1 else args.contrastive_class
    args.max_bag_size = None if args.max_bag_size == -1 else args.max_bag_size

    if 'no_bias' in args.lrp_params:
        args.lrp_params['no_bias'] = bool(args.lrp_params['no_bias'])

    return args


def save_json(save_dir, save_name, var_name):
    with open(os.path.join(save_dir, save_name+'.json'), 'w') as f:
        json.dump(var_name, f)


def main():
    args_user = get_args()
    print(json.dumps(vars(args_user), indent=4))

    with open(os.path.join(args_user.model_path, 'args.json')) as f:
        args_model = json.load(f)
        args_model['preload_data'] = args_user.preload_data

    print(json.dumps(args_model, indent=4))

    device = torch.device(args_user.device)

    # load the data_loader of interest based on the user argument args_user.dataset
    args_dataset = {**args_model, **vars(args_user)}
    none_datasets = [f'{set_name}_subsets' for set_name in ['train', 'val', 'test'] if set_name != args_user.dataset]
    for set_name in none_datasets:
        args_dataset[set_name] = None

    _, train_loader, _, val_loader, _, test_loader = DatasetFactory.build(args_dataset, args_dataset)
    data_loader = [loader for loader in [train_loader, val_loader, test_loader] if loader is not None][0]

    # define callback, model, classifier, xmodel, and xmodel_eval
    callback = Callback(
        schedule_lr=args_model['schedule_lr'], checkpoint_epoch=1, path_checkpoints=args_user.model_path,
        early_stop=args_model['early_stopping'], device=device)
    model, classifier = ModelFactory.build(args_model, device)
    model = callback.load_checkpoint(model, checkpoint=args_user.sel_checkpoint)
    xmodel = xModelFactory.build(model, vars(args_user))

    if args_user.explain_scores_path is not None:
        df_predictions = pd.read_csv(args_user.explain_scores_path)

    # the loop over the heatmap types of interest
    for heatmap_type in args_user.explanation_types:
        print(heatmap_type)

        if args_user.explain_scores_path is not None and heatmap_type in args_user.precomputed_heatmap_types:
            df_patch_scores = df_predictions
        else:
            df_patch_scores = None

        xmodel_eval = xMILEval(xmodel, classifier, heatmap_type=heatmap_type, scores_df=df_patch_scores)
        if args_user.flipping:
            torch.cuda.empty_cache()
            print(f'{args_user.approach} most relevant first ...')
            predicted_probs, false_preds, slide_ids, skipped = \
                xmodel_eval.patch_drop_or_add(data_loader, attribution_strategy='original',
                                              order='morf', approach=args_user.approach,
                                              strategy=args_user.strategy,
                                              max_bag_size=args_user.max_bag_size, verbose=False)

            save_json(args_user.results_dir,
                      f'probs_{heatmap_type}_{args_user.approach}_{args_user.dataset}', predicted_probs)
            save_json(args_user.results_dir, f'false_preds_{args_user.approach}_{args_user.dataset}', false_preds)
            save_json(args_user.results_dir, f'slide_ids_{args_user.approach}_{args_user.dataset}', slide_ids)
            save_json(args_user.results_dir, f'skipped_{args_user.approach}_{args_user.dataset}', skipped)


        if args_user.morl_abs:
            torch.cuda.empty_cache()
            print(f'{args_user.approach} most relevant last from absolute values ...')
            predicted_probs_morl_abs, false_preds, slide_ids, skipped = \
                xmodel_eval.patch_drop_or_add(data_loader, attribution_strategy='abs',
                                              order='morl', approach=args_user.approach,
                                              strategy=args_user.strategy,
                                              max_bag_size=args_user.max_bag_size, verbose=False)
            save_json(args_user.results_dir,
                      f'probs_{heatmap_type}_morl_abs_{args_user.approach}_{args_user.dataset}',
                      predicted_probs_morl_abs)
            save_json(args_user.results_dir, f'false_preds_{args_user.approach}_{args_user.dataset}', false_preds)
            save_json(args_user.results_dir, f'slide_ids_{args_user.approach}_{args_user.dataset}', slide_ids)
            save_json(args_user.results_dir, f'skipped_{args_user.approach}_{args_user.dataset}', skipped)

    if args_user.baseline:
        torch.cuda.empty_cache()
        print(f'random baseline ... ')
        xmodel_eval = xMILEval(xmodel, classifier, heatmap_type=None, scores_df=None)
        predicted_probs_baseline, false_preds, slide_ids, skipped = \
            xmodel_eval.patch_drop_or_add(data_loader, attribution_strategy='random',
                                          order='morf', approach=args_user.approach,
                                          strategy=args_user.strategy,
                                          max_bag_size=args_user.max_bag_size, verbose=False)

        save_json(args_user.results_dir, f'probs_baseline_{args_user.approach}_{args_user.dataset}',
                  predicted_probs_baseline)
        save_json(args_user.results_dir, f'false_preds_{args_user.approach}_{args_user.dataset}', false_preds)
        save_json(args_user.results_dir, f'slide_ids_{args_user.approach}_{args_user.dataset}', slide_ids)
        save_json(args_user.results_dir, f'skipped_{args_user.approach}_{args_user.dataset}', skipped)


if __name__ == '__main__':
    main()
