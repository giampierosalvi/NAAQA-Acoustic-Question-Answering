import os
import h5py
import numpy as np
from tqdm import tqdm
from collections import defaultdict
from random import shuffle

from data_interfaces.CLEAR_dataset import CLEARTokenizer
from utils.file import create_folder_if_necessary, save_json, read_json
from utils.processing import calc_mean_and_std, update_mean_in_config

import torch
import torch.nn as nn

from models.lr_finder import LRFinder
import matplotlib.pyplot as plt


def get_lr_finder_curves(model, device, train_dataloader, output_dated_folder, num_iter, optimizer, val_dataloader=None,
                         loss_criterion=nn.CrossEntropyLoss(), weight_decay_list=None, min_lr=1e-10):
    if type(weight_decay_list) != list:
        weight_decay_list = [0., 3e-1, 3e-2, 3e-3, 3e-4, 3e-5, 3e-6, 3e-7]

    # TODO : The order of the data probably affect the LR curves. Shuffling and doing multiple time should help

    # FIXME : What momentum value should we use for SGD ???
    # Force momentum to 0
    initial_optimizer_state_dict = optimizer.state_dict()
    optimizer.param_groups[0]['momentum'] = 0
    optimizer.param_groups[0]['lr'] = min_lr

    fig, ax = plt.subplots()
    lr_finder = LRFinder(model, optimizer, loss_criterion, device=device)

    for weight_decay in weight_decay_list:
        # Reset LR Finder and change weight decay
        lr_finder.reset(weight_decay=weight_decay)

        print(f"Learning Rate finder -- Running for {num_iter} batches with weight decay : {weight_decay:.5}")
        # FIXME : Should probably run with validation data?
        lr_finder.range_test(train_dataloader, val_loader=None, end_lr=100, num_iter=num_iter,
                             num_iter_val=100)

        fig, ax = lr_finder.plot(fig_ax=(fig, ax), legend_label=f"Weight Decay : {weight_decay:.5}", show_fig=False)

    filepath = "%s/%s" % (output_dated_folder, 'lr_finder_plot.png')
    fig.savefig(filepath)

    # Reset optimiser config
    optimizer.load_state_dict(initial_optimizer_state_dict)


def write_clear_mean_to_config(dataloader, device, current_config, config_file_path, overwrite_mean=False):
    assert os.path.isfile(config_file_path), f"Config file '{config_file_path}' doesn't exist"

    key = "clear_stats"

    if not overwrite_mean and 'preprocessing' in current_config and key in current_config['preprocessing'] \
       and type(current_config['preprocessing'][key]) == list:
        assert False, "CLEAR mean is already present in config."

    dataloader.dataset.keep_1_game_per_scene()

    mean, std = calc_mean_and_std(dataloader, device=device)

    update_mean_in_config(mean, std, config_file_path, current_config=current_config, key=key)


# >>> Feature Extraction
def images_to_h5(dataloaders, square_image, output_folder_name='preprocessed'):
    dataloaders_first_key = list(dataloaders.keys())[0]
    first_dataloader = dataloaders[dataloaders_first_key]

    assert first_dataloader.dataset.is_raw_img(), 'Input must be set to RAW in config to pre extract features.'

    data_path = first_dataloader.dataset.root_folder_path
    batch_size = first_dataloader.batch_size
    output_folder_path = '%s/%s' % (data_path, output_folder_name)

    # NOTE : When testing multiple dataset configurations, Images and questions are generated in separate folder and
    #        linked together so we don't have multiple copies of the dataset (And multiple preprocessing runs)
    #
    #        We use the default symlink to create the new folder at the correct destination so it is available
    #        to other configuration of the dataset (When extracting using different value of 'output_folder_name')
    #
    #        If "preprocessed" is not a symlink, 'output_folder_name' will be created in requested 'data_path'

    output_exist = os.path.exists(output_folder_path)
    preprocessed_default_folder_path = '%s/preprocessed' % data_path
    if not output_exist and os.path.exists(preprocessed_default_folder_path) and \
            os.path.islink(preprocessed_default_folder_path):

        # Retrieve paths from symlink
        default_link_value = os.readlink(preprocessed_default_folder_path)
        new_link_value = default_link_value.replace('preprocessed', output_folder_name)

        # Create folder in appropriate directory
        create_folder_if_necessary("%s/%s" % (data_path, new_link_value))

        # Create symlink in requested directory
        if not output_exist:
            os.symlink(new_link_value, output_folder_path)
    else:
        create_folder_if_necessary(output_folder_path)

    for set_type, dataloader in dataloaders.items():
        print("Creating H5 file from '%s' set" % set_type)
        output_filepath = '%s/%s_features.h5' % (output_folder_path, set_type)

        # Retrieve min & max dims of images
        max_width_id, height, max_width = dataloader.dataset.get_max_width_image_dims(return_scene_id=True)
        #game_id = dataloader.dataset.get_random_id_for_scene(max_width_id)
        #max_width_img = dataloader.dataset[game_id]['image'].unsqueeze(0)

        if square_image:
            max_dim = max(height, max_width)
            image_dim = [max_dim, max_dim, 3]
        else:
            image_dim = [height, max_width, 3]

        # Keep only 1 game per scene (We want to process every image only once)
        dataloader.dataset.keep_1_game_per_scene()

        nb_games = len(dataloader.dataset)

        with h5py.File(output_filepath, 'w') as f:
            # FIXME : Change dataset name ?
            h5_dataset = f.create_dataset('features', shape=[nb_games] + image_dim, dtype=np.float32)
            h5_idx2img = f.create_dataset('idx2img', shape=[nb_games], dtype=np.int32)
            h5_idx = 0
            for batch in tqdm(dataloader):
                # swap axis
                # numpy image: H x W x C
                # torch image: C X H X W
                # We want to save in numpy format
                images = batch['image'].numpy().transpose((0, 2, 3, 1))
                h5_dataset[h5_idx: h5_idx + batch_size] = images

                for i, scene_id in enumerate(batch['scene_id']):
                    h5_idx2img[h5_idx + i] = scene_id

                h5_idx += batch_size
        print("Images extracted successfully to '%s'" % output_filepath)


# >>> Feature Extraction
def extract_features(device, feature_extractor, dataloaders, output_folder_name='preprocessed'):
    dataloaders_first_key = list(dataloaders.keys())[0]
    first_dataloader = dataloaders[dataloaders_first_key]

    assert first_dataloader.dataset.is_raw_img(), 'Input must be set to RAW in config to pre extract features.'

    data_path = first_dataloader.dataset.root_folder_path
    batch_size = first_dataloader.batch_size
    output_folder_path = '%s/%s' % (data_path, output_folder_name)

    # NOTE : When testing multiple dataset configurations, Images and questions are generated in separate folder and
    #        linked together so we don't have multiple copies of the dataset (And multiple preprocessing runs)
    #
    #        We use the default symlink to create the new folder at the correct destination so it is available
    #        to other configuration of the dataset (When extracting using different value of 'output_folder_name')
    #
    #        If "preprocessed" is not a symlink, 'output_folder_name' will be created in requested 'data_path'

    output_exist = os.path.exists(output_folder_path)
    preprocessed_default_folder_path = '%s/preprocessed' % data_path
    if not output_exist and os.path.exists(preprocessed_default_folder_path) and \
            os.path.islink(preprocessed_default_folder_path):

        # Retrieve paths from symlink
        default_link_value = os.readlink(preprocessed_default_folder_path)
        new_link_value = default_link_value.replace('preprocessed', output_folder_name)

        # Create folder in appropriate directory
        create_folder_if_necessary("%s/%s" % (data_path, new_link_value))

        # Create symlink in requested directory
        if not output_exist:
            os.symlink(new_link_value, output_folder_path)
    else:
        create_folder_if_necessary(output_folder_path)

    # Set model to eval mode
    feature_extractor.eval()

    for set_type, dataloader in dataloaders.items():
        print("Extracting features from '%s' set" % set_type)
        output_filepath = '%s/%s_features.h5' % (output_folder_path, set_type)

        # Retrieve min & max dims of images
        max_width_id, height, max_width = dataloader.dataset.get_max_width_image_dims(return_scene_id=True)
        game_id = dataloader.dataset.get_random_id_for_scene(max_width_id)
        max_width_img = dataloader.dataset[game_id]['image'].unsqueeze(0).to(device)
        feature_extractor_output_shape = feature_extractor.get_output_shape(max_width_img, channel_first=False)

        # Keep only 1 game per scene (We want to process every image only once)
        dataloader.dataset.keep_1_game_per_scene()

        nb_games = len(dataloader.dataset)

        with h5py.File(output_filepath, 'w') as f:
            # FIXME : Find a way to have variable size. MaxShape is not the answer
            #         We can use --pad_to_largest image, save a dataset of padding and remove padding when retrieving <<-- This won't work, the output shape is different after passing throught the feature extractor. Padding at this level will have a big impact
            h5_dataset = f.create_dataset('features', shape=[nb_games] + feature_extractor_output_shape, dtype=np.float32)
            h5_idx2img = f.create_dataset('idx2img', shape=[nb_games], dtype=np.int32)
            h5_idx = 0
            for batch in tqdm(dataloader):
                images = batch['image'].to(device)

                with torch.set_grad_enabled(False):
                    features = feature_extractor(images).detach().cpu().numpy()

                # swap axis
                # numpy image: H x W x C
                # torch image: C X H X W
                # We want to save in numpy format
                features = features.transpose((0, 2, 3, 1))

                h5_dataset[h5_idx: h5_idx + batch_size] = features

                for i, scene_id in enumerate(batch['scene_id']):
                    h5_idx2img[h5_idx + i] = scene_id

                h5_idx += batch_size
        print("Features extracted succesfully to '%s'" % output_filepath)


# >>> Dictionary Creation (For word tokenization)
def create_dict_from_questions(dataset, word_min_occurence=1, dict_filename='dict.json', force_all_answers=False,
                               output_folder_name='preprocessed', start_end_tokens=True):
    games = dataset.games

    word2i = {'<padding>': 0,
              '<unk>': 1
              }
    word_index = max(word2i.values()) + 1

    if start_end_tokens:
        word2i['<start>'] = word_index
        word2i['<end>'] = word_index + 1
        word_index += 2

    answer2i = {  #'<padding>': 0,        # FIXME : Why would we need padding in the answers ?
        '<unk>': 0  # FIXME : We have no training example with unkonwn answer. Add Switch to remove unknown answer
    }
    answer_index = max(answer2i.values()) + 1

    answers = [k.lower() for k in dataset.answer_counter.keys()]
    word2occ = defaultdict(int)

    tokenizer = CLEARTokenizer.get_tokenizer_inst()
    forbidden_tokens = [",", "?"]

    # Tokenize questions
    for i in range(len(games)):
        game = dataset.get_game(i)
        input_tokens = [t.lower() for t in tokenizer.tokenize(game['question'])]
        for tok in input_tokens:
            if tok not in forbidden_tokens:
                word2occ[tok] += 1

    # Sort tokens then shuffle then to keep control over the order (to enhance reproducibility)
    sorted_words = sorted(word2occ.items(), key=lambda x: x[0])
    shuffle(sorted_words)

    for word_occ in sorted_words:
        if word_occ[1] >= word_min_occurence:
            word2i[word_occ[0]] = word_index
            word_index += 1

    sorted_answers = sorted(answers)
    shuffle(sorted_answers)
    # parse the answers
    for answer in sorted_answers:
        answer2i[answer] = answer_index
        answer_index += 1

    if force_all_answers:
        all_answers = read_json(dataset.root_folder_path, 'attributes.json')

        all_answers = [a for answers in all_answers.values() for a in answers]

        padded_answers = []

        for answer in all_answers:
            if answer not in answer2i:
                answer2i[answer] = answer_index
                answer_index += 1
                padded_answers.append(answer)

        print("Padded dict with %d missing answers : " % len(padded_answers))
        print(padded_answers)

    print("Number of words: {}".format(len(word2i)))
    print("Number of answers: {}".format(len(answer2i)))

    preprocessed_folder_path = os.path.join(dataset.root_folder_path, output_folder_name)

    if not os.path.isdir(preprocessed_folder_path):
        os.mkdir(preprocessed_folder_path)

    save_json({
            'word2i': word2i,
            'answer2i': answer2i
        }, preprocessed_folder_path, dict_filename)


if __name__ == "__main__":
    print("To run preprocessing, use main.py --preprocessing (or --create_dict, --feature_extract for individual steps)")
    exit(1)
