import os
from datetime import datetime, timedelta
import re

import pandas as pd
import numpy as np

from utils.file import read_json, save_json


def to_float(string):
    # Conversion with None handling
    return float(string) if string else None


def to_int(string):
    # Conversion with None handling
    return int(string) if string else None


def get_experiments(experiment_result_path, prefix=None):
    experiments = []

    for exp_folder in os.listdir(experiment_result_path):
        exp_folder_path = f'{experiment_result_path}/{exp_folder}'

        if not os.path.isdir(exp_folder_path):
            continue

        if prefix and prefix not in exp_folder:
            continue

        for date_folder in os.listdir(exp_folder_path):
            exp_dated_folder_path = f'{exp_folder_path}/{date_folder}'

            if date_folder == 'latest':
                # We skip the 'latest' symlink
                continue

            if 'best' not in os.listdir(exp_dated_folder_path):
                # Failed experiment. Was stopped before first epoch could be saved
                print(f"Failed experiment -- {exp_dated_folder_path}")
                continue

            # Load arguments
            arguments = read_json(f"{exp_dated_folder_path}/arguments.json")

            # Retrieve Prefix, nb_scene and nb_question_per_scene from version name
            matches = re.match('(.*)_(\d+)k_(\d+)_(.*)', arguments['version_name'])

            if not matches:
                continue

            matches = matches.groups()

            # A random id might be appended to the experiment folder when uploaded to drive, remove it
            date_folder = re.sub(r'-\d+$', '', date_folder)

            experiment = {
                'prefix': matches[0],
                'nb_scene': to_int(matches[1]) * 1000,
                'nb_q_per_scene': to_int(matches[2]),
                'config': arguments['config_path'].replace('config/','').replace('/','_').replace('.json', ''),
                'nb_epoch': arguments['nb_epoch'],
                'stop_accuracy': arguments['stop_at_val_acc'],
                'random_seed': arguments['random_seed'],
                'date': datetime.strptime(date_folder, '%Y-%m-%d_%Hh%M'),
                'folder': exp_folder
            }

            additional_note = arguments['output_name_suffix'].replace(f'_{experiment["nb_epoch"]}_epoch', '').replace(experiment['config'], '').replace(f'_{experiment["random_seed"]}', '').replace(f"_stop_at_{experiment['stop_accuracy']}", '').replace('_resnet_extractor', '').replace('config_', '')

            # Trim note
            if len(additional_note) > 0:
                if additional_note[0] == '_':
                    additional_note = additional_note[1:]

                if additional_note[-1] == '_':
                    additional_note = additional_note[:-1]
            else:
                additional_note = None

            experiment['note'] = additional_note

            experiment['nb_sample'] = experiment['nb_scene'] * experiment['nb_q_per_scene']

            # Load experiment stats
            epoch_stats = read_json(f'{exp_dated_folder_path}/stats.json')

            experiment['nb_epoch_runned'] = len(epoch_stats)
            experiment['nb_epoch_trained'] = to_int(epoch_stats[0]['epoch'].split('_')[-1])
            experiment['best_val_acc'] = to_float(epoch_stats[0]['val_acc'])
            experiment['best_val_loss'] = to_float(epoch_stats[0]['val_loss'])
            experiment['train_acc'] = to_float(epoch_stats[0]['train_acc'])
            experiment['train_loss'] = to_float(epoch_stats[0]['train_loss'])

            epoch_stats_chronological = sorted(epoch_stats, key=lambda x: to_int(x['epoch'].split('_')[1]))
            experiment['all_train_acc'] = []
            experiment['all_train_loss'] = []
            experiment['all_val_acc'] = []
            experiment['all_val_loss'] = []
            experiment['train_time'] = timedelta(0)
            epoch_times = []
            experiment['0.6_at_epoch'] = None
            experiment['0.7_at_epoch'] = None
            experiment['0.8_at_epoch'] = None
            experiment['0.9_at_epoch'] = None

            for stat in epoch_stats_chronological:
                experiment['all_train_acc'].append(to_float(stat['train_acc']))
                experiment['all_train_loss'].append(to_float(stat['train_loss']))
                experiment['all_val_acc'].append(to_float(stat['val_acc']))
                experiment['all_val_loss'].append(to_float(stat['val_loss']))

                parsed_time = datetime.strptime(stat['train_time'], "%H:%M:%S.%f")
                epoch_time = timedelta(hours=parsed_time.hour, minutes=parsed_time.minute, seconds=parsed_time.second,
                                       microseconds=parsed_time.microsecond)
                epoch_times.append(epoch_time)
                experiment['train_time'] += epoch_time

                epoch_idx = to_int(stat['epoch'].split('_')[1])
                val_acc = to_float(stat['val_acc'])

                if experiment['0.6_at_epoch'] is None and val_acc >= 0.6:
                    experiment['0.6_at_epoch'] = epoch_idx
                elif experiment['0.7_at_epoch'] is None and val_acc >= 0.7:
                    experiment['0.7_at_epoch'] = epoch_idx
                elif experiment['0.8_at_epoch'] is None and val_acc >= 0.8:
                    experiment['0.8_at_epoch'] = epoch_idx
                elif experiment['0.9_at_epoch'] is None and val_acc >= 0.9:
                    experiment['0.9_at_epoch'] = epoch_idx

            experiment['mean_epoch_time'] = np.mean(epoch_times)

            # Load test set results
            test_result_filepath = f"{exp_dated_folder_path}/test_stats.json"
            if os.path.isfile(test_result_filepath):
                test_stats = read_json(f"{exp_dated_folder_path}/test_stats.json")
                experiment['test_version'] = test_stats['version_name']
                experiment['test_acc'] = to_float(test_stats['accuracy'])
                experiment['test_loss'] = to_float(test_stats['loss'])
            else:
                experiment['test_version'] = None
                experiment['test_acc'] = None
                experiment['test_loss'] = None

            if experiment['nb_epoch_runned'] < experiment['nb_epoch']:
                # TODO : Check stopped_early.json
                if experiment['stop_accuracy'] and experiment['best_val_acc'] >= experiment['stop_accuracy']:
                    experiment['stopped_early'] = 'stop_threshold'
                elif experiment['test_acc'] is None:
                    experiment['stopped_early'] = 'RUNNING'
                else:
                    experiment['stopped_early'] = 'not_learning'
            else:
                experiment['stopped_early'] = 'NO'

            # Load number of params from model_summary
            experiment['total_nb_param'], experiment['nb_trainable_param'], experiment['nb_non_trainable_param'] = get_nb_param_from_summary(f'{exp_dated_folder_path}/model_summary.txt')

            experiment['batch_size'] = arguments['batch_size']
            experiment['resnet_features'] = arguments['conv_feature_input']

            img_arguments = arguments

            if arguments['h5_image_input']:
                # Copy preprocessed arguments if not in results
                local_preprocessed_arguments = f"{exp_dated_folder_path}/preprocessed_arguments.json"
                preprocessed_argument_path = f"{arguments['data_root_path']}/{arguments['version_name']}/{arguments['preprocessed_folder_name']}/arguments.json"

                if os.path.exists(local_preprocessed_arguments):
                    # Preprocessed arguments stored in the results folder
                    img_arguments = read_json(local_preprocessed_arguments)
                elif os.path.exists(preprocessed_argument_path):
                    # Preprocessed arguments stored in the data folder
                    img_arguments = read_json(preprocessed_argument_path)

                    save_json(img_arguments, local_preprocessed_arguments)
                #else:
                    #print(f"Was unable to retrieve preprocessing arguments for version {arguments['version_name']}")

                # Copy preprocessed data stats (mean, std, min, max) if not in results
                local_preprocesses_stats = f"{exp_dated_folder_path}/preprocessed_data_stats.json"
                preprocessed_stats_path = f"{arguments['data_root_path']}/{arguments['version_name']}/{arguments['preprocessed_folder_name']}/clear_stats.json"

                if not os.path.exists(local_preprocesses_stats) and os.path.exists(preprocessed_stats_path):
                    preprocessed_stats = read_json(preprocessed_stats_path)

                    save_json(preprocessed_stats, local_preprocesses_stats)
                #else:
                    #print(f"Was unable to retrieve dataset statistics for {arguments['version_name']} -- {arguments['preprocessed_folder_name']}")

            experiment['input_type'] = img_arguments['input_image_type']

            experiment['n_fft'] = img_arguments['spectrogram_n_fft'] if 'spectrogram_n_fft' in img_arguments else None

            experiment['hop_length'] = img_arguments['spectrogram_hop_length'] if 'spectrogram_hop_length' in img_arguments else None

            experiment['keep_freq_point'] = img_arguments['spectrogram_keep_freq_point'] if 'spectrogram_keep_freq_point' in img_arguments else None

            experiment['n_mels'] = img_arguments['spectrogram_n_mels'] if 'mel_spectrogram' in img_arguments and 'spectrogram_n_mels' in img_arguments and img_arguments['mel_spectrogram'] else None

            experiment['resample_audio'] = img_arguments['resample_audio_to'] if 'resample_audio_to' in img_arguments else None

            experiment['norm_zero_one'] = img_arguments['normalize_zero_one']
            experiment['norm_clear_stats'] = img_arguments['normalize_with_clear_stats']

            experiment['pad_to_largest'] = img_arguments['pad_to_largest_image']
            experiment['resized_height'] = to_int(img_arguments['img_resize_height']) if img_arguments['resize_img'] else None
            experiment['resized_width'] = to_int(img_arguments['img_resize_width']) if img_arguments['resize_img'] else None

            # Load dict
            exp_dict = read_json(f'{exp_dated_folder_path}/dict.json')
            experiment['nb_answer'] = len(exp_dict['answer2i'])


            # Load timing

            # Load git-revision
            with open(f'{exp_dated_folder_path}/git.revision', 'r') as f:
                experiment['git_revision'] = f.readlines()[0].replace('\n', '')

            # Load config
            config_filepath = f'config/{experiment["config"]}.json'

            if not os.path.exists(config_filepath):
                # Config file doesn't exist on local instance, use the one in the exp_dated_folder
                config_filename = [f for f in os.listdir(exp_dated_folder_path) if 'config' in f and '.json' in f]

                if len(config_filename) == 0:
                    raise FileNotFoundError(f"No config file in '{exp_dated_folder_path}'")

                config_filepath = f"{exp_dated_folder_path}/{config_filename[0]}"

            config = read_json(config_filepath)

            experiment['word_embedding_dim'] = to_int(config['question']['word_embedding_dim'])
            experiment['rnn_state_size'] = to_int(config['question']['rnn_state_size'])
            experiment['extractor_type'] = config['image_extractor']['type']
            experiment['extractor_out_chan'] = to_int(config['image_extractor']['out'][-1]) if type(config['image_extractor']['out']) == list else config['image_extractor']['out']
            experiment['extractor_filters'] = config['image_extractor']['out']

            if experiment['extractor_type'] in ['film_original', 'conv']:
                experiment['extractor_nb_block'] = len(config['image_extractor']['kernels'])
                experiment['extractor_projection_size'] = None
            elif not 'resnet' in experiment['extractor_type']:
                experiment['extractor_nb_block'] = len(config['image_extractor']['freq_kernels'])

                if len(config['image_extractor']['out']) > experiment['extractor_nb_block']:
                    experiment['extractor_projection_size'] = to_int(config['image_extractor']['out'][-1])
                    experiment['extractor_filters'] = experiment['extractor_filters'][:-1]
                else:
                    experiment['extractor_projection_size'] = None

            experiment['stem_out_chan'] = to_int(config['stem']['conv_out'])
            experiment['nb_resblock'] = len(config['resblock']['conv_out'])
            experiment['resblocks_out_chan'] = to_int(config['resblock']['conv_out'][-1])
            experiment['classifier_projection_out'] = to_int(config['classifier']['projection_size'])
            experiment['classifier_type'] = config['classifier']['type']
            experiment['classifier_global_pool'] = config['classifier']['global_pool_type']
            experiment['optimizer_type'] = config['optimizer']['type']
            experiment['optimizer_lr'] = to_float(config['optimizer']['learning_rate'])
            experiment['optimizer_weight_decay'] = to_float(config['optimizer']['weight_decay'])
            experiment['dropout_drop_prob'] = to_float(config['optimizer']['dropout_drop_prob'])

            if 'conv_out' not in config['classifier'] or experiment['classifier_type'] == 'conv':
                experiment['classifier_conv_out'] = None
            else:
                experiment['classifier_conv_out'] = to_int(config['classifier']['conv_out'])

            # Gpu name
            gpu_name_filepath = f"{exp_dated_folder_path}/gpu.json"
            if os.path.exists(gpu_name_filepath):
                experiment['gpu_name'] = read_json(gpu_name_filepath)['gpu_name']
            else:
                experiment['gpu_name'] = None

            experiments.append(experiment)

    experiments_df = pd.DataFrame(experiments,
                                  columns=list(experiments[0].keys()))
                                  #columns=['prefix', 'nb_sample', 'nb_scene', 'nb_q_per_scene', 'config', 'nb_epoch',
                                  #         'nb_epoch_runned', 'stop_accuracy', 'best_val_acc', 'best_val_loss',
                                  #         'test_acc', 'test_loss', 'train_acc', 'train_loss', 'stopped_early',
                                  #         '0.6_at_epoch', '0.7_at_epoch', '0.8_at_epoch', '0.9_at_epoch',
                                  #         'batch_size', 'resnet_features', 'nb_trainable_param', 'test_version',
                                  #         'random_seed', 'date', 'total_nb_param', 'nb_non_trainable_param',
                                  #         'word_embedding_dim', 'rnn_state_size', 'extractor_type', 'stem_out_chan',
                                  #         'nb_resblock', 'resblocks_out_chan', 'classifier_conv_out_chan',
                                  #         'classifier_type', 'classifier_global_pool', 'optimizer_type', 'nb_answer',
                                  #         'optimizer_lr', 'optimizer_weight_decay', 'dropout_drop_prob',
                                  #         'git_revision', 'pad_to_largest', 'resized_height', 'resized_width',
                                  #         'all_train_acc', 'all_train_loss', 'all_val_acc', 'all_val_loss',
                                  #         'train_time', 'mean_epoch_time', 'gpu_name', 'folder', 'note'
                                  #         ]
                                  #)
    return experiments_df


def get_nb_param_from_summary(summary_filepath):
    with open(summary_filepath, 'r') as f:
        summary_lines = f.readlines()

        # Retrive lines containing 'params'. First is total params, second trainable params, third non-trainable
        nb_params = [int(l.split(':')[1].strip().replace(',', '')) for l in summary_lines if 'params' in l]

    return tuple(nb_params)
