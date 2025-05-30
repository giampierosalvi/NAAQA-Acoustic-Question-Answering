from datetime import datetime
from utils.generic import is_date_string
import os


# Argument handling
def get_args_task_flags_paths(args):
    validate_arguments(args)
    task = get_task_from_args(args)
    paths = get_paths_from_args(task, args)
    flags = create_flags_from_args(task, args)
    update_arguments(args, task, paths, flags)

    return args, task, flags, paths


def validate_arguments(args):
    mutually_exclusive_params = [args['training'], args['inference'], args['create_dict'],
                                 args['visualize_gamma_beta'], args['visualize_grad_cam'], args['lr_finder'],
                                 args['random_answer_baseline'], args['random_weight_baseline'], args['prepare_images'],
                                 args['notebook_data_analysis'], args['notebook_model_inference'],
                                 args['tf_weight_transfer']]

    assert sum(mutually_exclusive_params) == 1, \
        "[ERROR] Can only do one task at a time " \
        "(--training, --inference, --visualize_gamma_beta, --create_dict, --visualize_grad_cam " \
        "--prepare_images, --lr_finder, --random_answer_baseline, " \
        "--random_weight_baseline, --notebook_data_analysis, --notebook_model_inference)"

    assert not args['continue_training'] or (args['training'] and args['continue_training']), \
        "[ERROR] Must be in --training mode for --continue_training"

    assert sum([args['pad_to_largest_image'], args['pad_per_batch']]) <= 1, \
        '[ERROR] --pad_to_largest_image and --pad_per_batch can\'t be used together'

    assert sum([args['normalize_with_imagenet_stats'], args['normalize_with_clear_stats']]) <= 1, \
        '[ERROR] --normalize_with_imagenet_stats and --normalize_with_clear_stats can\'t be used together'

    assert sum([args['only_text_modality'], args['only_audio_modality']]) <= 1, \
        '[ERROR] --only_text_modality and --only_audio_modality can\'t be used together'


def create_flags_from_args(task, args):
    flags = {}

    flags['restore_model_weights'] = args['continue_training'] or args['film_model_weight_path'] is not None \
                                     or task in ['inference', 'visualize_grad_cam', 'notebook_model_inference']
    flags['use_tensorboard'] = 'train' in task
    flags['create_loss_criterion'] = task in ['training', 'lr_finder', 'inference']
    flags['create_optimizer'] = task in ['training', 'lr_finder']
    flags['force_sgd_optimizer'] = task == 'lr_finder' or args['cyclical_lr']
    flags['load_dataset_extra_stats'] = task.startswith('notebook')
    flags['create_output_folder'] = task not in ['create_dict', 'feature_extract',
                                                 'write_clear_mean_to_config'] and not task.startswith('notebook')
    flags['instantiate_model'] = task in ['training',
                                          'inference',
                                          'visualize_grad_cam',
                                          'lr_finder',
                                          'random_weight_baseline',
                                          'notebook_model_inference',
                                          'tf_weight_transfer']

    return flags


def get_paths_from_args(task, args):
    paths = {}

    paths["output_name"] = args['version_name'] + "_" + args['output_name_suffix'] if args['output_name_suffix'] else args['version_name']
    paths["data_path"] = "%s/%s" % (args['data_root_path'], args['version_name'])
    paths["output_task_folder"] = "%s/%s" % (args['output_root_path'], task)
    paths["output_experiment_folder"] = "%s/%s" % (paths["output_task_folder"], paths["output_name"])
    paths["current_datetime"] = datetime.now()
    paths["current_datetime_str"] = paths["current_datetime"].strftime("%Y-%m-%d_%Hh%M")
    paths["output_dated_folder"] = "%s/%s" % (paths["output_experiment_folder"], paths["current_datetime_str"])

    return paths


def get_task_from_args(args):
    tasks = ['training', 'inference', 'visualize_gamma_beta', 'visualize_grad_cam', 'prepare_images',
             'create_dict', 'lr_finder', 'random_weight_baseline',
             'random_answer_baseline', 'notebook_data_analysis', 'notebook_model_inference', 'tf_weight_transfer']

    for task in tasks:
        if task in args and args[task]:
            return task

    assert False, "Arguments don't specify task"


def update_arguments(args, task, paths, flags):
    if args['h5_image_input']:
        args['input_image_type'] = "raw_h5"
    elif args['audio_input'] or (task.startswith('notebook') and 'audio' in args['version_name']):
        args['input_image_type'] = "audio"
    else:
        args['input_image_type'] = "raw"

    # Revert to using only 1 worker. More workers give unreproductible results..
    #   This might be cause by the fact that each worker get a different random seed. TODO : Investigate this
    args['nb_dataloader_worker'] = 1
    #args['nb_dataloader_worker'] = 2

    #if args['input_image_type'] == 'raw':
        # Default values when in RAW mode
    #    args['nb_dataloader_worker'] = 3

    #if args['resnet_feature_extractor']:
    #    args['nb_dataloader_worker'] = 4

    if args['do_transforms_on_gpu']:
        args['nb_dataloader_worker'] = 0

    args['normalize_zero_one'] = args['normalize_zero_one'] and not args['keep_image_range']

    args['dict_folder'] = args['preprocessed_folder_name'] if args['dict_folder'] is None else args['dict_folder']
    if args['dict_file_path'] is None:
        args['dict_file_path'] = "%s/%s/dict.json" % (paths["data_path"], args['dict_folder'])

    if flags['restore_model_weights']:
        if args['continue_training'] and args['film_model_weight_path'] is None:
            # Use latest by default when continuing training
            args['film_model_weight_path'] = 'latest'

        assert args['film_model_weight_path'] is not None, 'Must provide path to model weights to ' \
                                                           'do inference or to continue training.'

        # If path specified is a date, we construct the path to the best model weights for the specified run
        splitted_weight_path = args['film_model_weight_path'].split("/")
        if splitted_weight_path[0].startswith("output") or len(splitted_weight_path[0]) == 0:
            base_path = args['film_model_weight_path']
        else:
            base_path = f"{args['output_root_path']}/training/{paths['output_name']}/{args['film_model_weight_path']}"
        # Note : We might redo some epoch when continuing training because the 'best' epoch is not necessarily the last
        suffix = "best/model.pt.tar"

        if is_date_string(args['film_model_weight_path']):
            args['film_model_weight_path'] = "%s/%s" % (base_path, suffix)
        elif args['film_model_weight_path'] == 'latest':
            # The 'latest' symlink will be overriden by this run (If continuing training).
            # Use real path of latest experiment
            symlink_value = os.readlink(base_path)
            clean_base_path = base_path[:-(len(args['film_model_weight_path']) + 1)]
            args['film_model_weight_path'] = '%s/%s/%s' % (clean_base_path, symlink_value, suffix)

        config_base_path = base_path.split('/')
        if config_base_path[-1] == "model.pt.tar":
            config_base_path = config_base_path[:-2]
        else:
            args['film_model_weight_path'] = "%s/%s" % (base_path, suffix)

        args['config_path'] = f"{'/'.join(config_base_path)}/config_raw_h5_input.json"   # FIXME : Ideally, we would look in the directory and find the "config*.json" file instead of hardcoding...

    args['clear_stats_file_path'] = f"{args['data_root_path']}/{args['version_name']}/{args['preprocessed_folder_name']}/clear_stats.json"

    # By default the start_epoch should is 0. Will only be modified if loading from checkpoint
    args["start_epoch"] = 0


def get_feature_extractor_config_from_args(args):
    if args['no_feature_extractor']:
        return None
    else:
        return {'version': 101, 'layer_index': args['feature_extractor_layer_index']}  # Idx 6 -> Block3/unit22