import shutil
from datetime import datetime

import subprocess
from tqdm import tqdm
import torch
import torch.nn as nn
from torch.optim.lr_scheduler import CyclicLR

from models.CLEAR_film_model import CLEAR_FiLM_model
from models.metrics import calc_f1_score
from utils.generic import is_date_string, sort_stats, save_training_stats
from utils.random import get_random_state, set_random_state
from utils.processing import process_predictions, process_gamma_beta
from utils.file import create_folder_if_necessary, save_json, save_gamma_beta_h5


def prepare_model(args, flags, paths, dataloaders, device, model_config, input_image_torch_shape,
                  feature_extractor_config=None):
    print("Creating model")
    # Retrieve informations to instantiate model
    train_dataset = dataloaders['train'].dataset
    nb_words, nb_answers = train_dataset.get_token_counts()
    padding_token = train_dataset.get_padding_token()

    film_model = CLEAR_FiLM_model(model_config, input_image_channels=input_image_torch_shape[0],
                                  nb_words=nb_words, nb_answers=nb_answers,
                                  sequence_padding_idx=padding_token,
                                  feature_extraction_config=feature_extractor_config)

    # Default values
    optimizer, loss_criterion, scheduler = None, None, None
    trainable_parameters = filter(lambda p: p.requires_grad, film_model.parameters())

    if flags['create_optimizer']:
        if model_config['optimizer'].get('type', '') == 'sgd' or flags["force_sgd_optimizer"]:
            optimizer = torch.optim.SGD(trainable_parameters, lr=model_config['optimizer']['learning_rate'],
                                        momentum=model_config['optimizer']['momentum'],
                                        weight_decay=model_config['optimizer']['weight_decay'])
        else:
            optimizer = torch.optim.Adam(trainable_parameters, lr=model_config['optimizer']['learning_rate'],
                                         weight_decay=model_config['optimizer']['weight_decay'])

    if flags['create_loss_criterion']:
        loss_criterion_tmp = nn.CrossEntropyLoss()

        if args['f1_score']:
            def loss_criterion(outputs, answers):
                loss = loss_criterion_tmp(outputs, answers)
                _, preds = torch.max(outputs, 1)

                return loss + (1 - calc_f1_score(preds, answers))
        else:
            loss_criterion = loss_criterion_tmp

    if args['cyclical_lr']:
        base_lr = model_config['optimizer']['cyclical']['base_learning_rate']
        max_lr = model_config['optimizer']['cyclical']['max_learning_rate']
        base_momentum = model_config['optimizer']['cyclical']['base_momentum']
        max_momentum = model_config['optimizer']['cyclical']['max_momentum']

        total_nb_steps = args['nb_epoch'] * len(dataloaders['train'])

        cycle_length = model_config['optimizer']['cyclical']['cycle_length']

        if type(cycle_length) == int:
            # Cycle length define the number of step in the cycle
            cycle_step = cycle_length
        elif type(cycle_length) == float:
            # Cycle length is a ratio of the total nb steps
            cycle_step = int(total_nb_steps * cycle_length)

        print(f"Using cyclical LR : ({base_lr:.5},{max_lr:.5})  Momentum ({base_momentum:.5}, {max_momentum:.5})")
        print(f"Total nb steps : {total_nb_steps} ({args['nb_epoch']} epoch)  -- Nb steps per cycle : {cycle_step} "
              f"({cycle_step / len(dataloaders['train'])} epoch)")

        scheduler = CyclicLR(optimizer, base_lr=base_lr,
                             max_lr=max_lr,
                             step_size_up=cycle_step // 2,
                             base_momentum=base_momentum,
                             max_momentum=max_momentum)

    if flags['restore_model_weights']:
        print(f"Restoring model weights from '{args['film_model_weight_path']}'")

        checkpoint = torch.load(args['film_model_weight_path'], map_location=device)

        if 'epoch' in checkpoint:
            args['start_epoch'] = checkpoint['epoch'] + 1

        if 'rng_state' in checkpoint:
            if device != 'cpu':
                if 'torch' in checkpoint['rng_state']:
                    checkpoint['rng_state']['torch'] = checkpoint['rng_state']['torch'].cpu()

                if 'torch_cuda' in checkpoint['rng_state']:
                    checkpoint['rng_state']['torch_cuda'] = checkpoint['rng_state']['torch_cuda'].cpu()

            set_random_state(checkpoint['rng_state'])

        # We need non-strict because feature extractor weight are not included in the saved state dict
        film_model.load_state_dict(checkpoint['model_state_dict'], strict=False)

        if optimizer and "optimizer_state_dict" in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

        if scheduler and 'scheduler_state_dict' in checkpoint:
            scheduler.load_state_dict(checkpoint['scheduler_state_dict'])

    if device != 'cpu':
        if args['perf_over_determinist']:
            torch.backends.cudnn.benchmark = True
            torch.backends.cudnn.deterministic = False
        else:
            torch.backends.cudnn.benchmark = False
            torch.backends.cudnn.deterministic = True

    film_model.to(device)

    print("Model ready to run\n")

    return film_model, optimizer, loss_criterion, scheduler


def process_dataloader(is_training, device, model, dataloader, criterion=None, optimizer=None, scheduler=None,
                       gamma_beta_path=None, write_to_file_every=500, epoch_id=0, tensorboard=None):
    # Model should already have been copied to the GPU at this point (If using GPU)
    assert (is_training and criterion is not None and optimizer is not None) or not is_training

    if is_training:
        model.train()
    else:
        model.eval()

    dataset_size = len(dataloader.dataset)
    batch_size = dataloader.batch_size
    running_loss = 0.0
    running_corrects = 0

    processed_predictions = []
    processed_gammas_betas = []
    all_questions = []
    nb_written = 0

    for batch_idx, batch in enumerate(tqdm(dataloader)):
        #mem_trace.report('Batch %d/%d - Epoch %d' % (i, dataloader.batch_size, epoch))
        images = batch['image'].to(device)
        questions = batch['question'].to(device)
        answers = batch['answer'].to(device)
        seq_lengths = batch['seq_length'].to(device)

        # Those are not processed by the network, only used to create statistics. Therefore, no need to copy to GPU
        questions_id = batch['id']
        scenes_id = batch['scene_id']
        images_padding = batch['image_padding']

        if is_training:
            # zero the parameter gradients
            optimizer.zero_grad()

        with torch.set_grad_enabled(is_training):
            outputs, outputs_softmax = model(questions, seq_lengths, images, pack_sequence=True)
            _, preds = torch.max(outputs, 1)
            if criterion:
                loss = criterion(outputs, answers)

            if is_training:
                # backward + optimize only if in training phase
                loss.backward()
                optimizer.step()

                if scheduler:
                    scheduler.step()

        batch_processed_predictions = process_predictions(dataloader.dataset, preds.tolist(), answers.tolist(),
                                                          questions_id.tolist(), scenes_id.tolist(),
                                                          outputs_softmax.tolist(), images_padding.tolist())

        processed_predictions += batch_processed_predictions

        # TODO : Add config to log only specific things
        if tensorboard and tensorboard['writer'] and tensorboard['options'] and tensorboard['options']['save_images']:
            # FIXME: Find a way to show original input images in tensorboard (Could save a list of scene ids and add them to tensorboard after the epoch, check performance cost -- Image loading etc)
            if dataloader.dataset.is_raw_img():
                # TODO : Tag img before adding to tensorboard ? -- This can be done via .add_image_with_boxes()
                for image in batch['image']:
                    tensorboard['writer'].add_image('Inputs/images', image, epoch_id)

            all_questions += batch['question'].tolist()

        if gamma_beta_path is not None:
            gammas, betas = model.get_gammas_betas()
            processed_gammas_betas += process_gamma_beta(batch_processed_predictions, gammas, betas)

            if batch_idx % write_to_file_every == 0 and batch_idx != 0:
                nb_written += save_gamma_beta_h5(processed_gammas_betas, dataloader.dataset.set, gamma_beta_path,
                                                 nb_vals=dataset_size, start_idx=nb_written)
                processed_gammas_betas = []

        # statistics
        if criterion:
            running_loss += loss.item() * dataloader.batch_size
        running_corrects += torch.sum(preds == answers.data).item()

    nb_left_to_write = len(processed_gammas_betas)
    if gamma_beta_path is not None and nb_left_to_write > 0:
        save_gamma_beta_h5(processed_gammas_betas, dataloader.dataset.set, gamma_beta_path, nb_vals=dataset_size,
                           start_idx=nb_written)

    epoch_loss = running_loss / dataset_size
    epoch_acc = running_corrects / dataset_size

    # TODO : Add config to log only specific things
    if tensorboard and tensorboard['writer']:
        if tensorboard['options'] and tensorboard['options']['save_texts']:
            log_text = ""
            for question, processed_prediction in zip(all_questions, processed_predictions):
                # FIXME: Tokenizer might not be instantiated --- We probably wouldn't be logging in tensorboard..
                decoded_question = dataloader.dataset.tokenizer.decode_question(question, remove_padding=True)
                log_text += f"{processed_prediction['correct']}//{processed_prediction['correct_answer_family']} "
                log_text += f"{decoded_question} -- {processed_prediction['ground_truth']} "
                log_text += f"[[{processed_prediction['prediction']} - {processed_prediction['confidence']}]]  \n"

            tensorboard['writer'].add_text('Inputs/Text', log_text, epoch_id)

        tensorboard['writer'].add_scalar('Results/Loss', epoch_loss, global_step=epoch_id)
        tensorboard['writer'].add_scalar('Results/Accuracy', epoch_acc, global_step=epoch_id)

    return epoch_loss, epoch_acc, processed_predictions


def train_model(device, model, dataloaders, output_folder, criterion, optimizer, scheduler=None,
                nb_epoch=25, nb_epoch_to_keep=None, start_epoch=0, tensorboard=None):

    assert nb_epoch > 0, "Must train for at least 1 epoch"

    if tensorboard is None:
        tensorboard = {'writers': {'train': None, 'val': None}, 'options': None}
    else:
        assert 'train' in tensorboard['writers'] and 'val' in tensorboard['writers'], 'Must provide all tensorboard writers.'

    tensorboard_per_set = {'writer': None, 'options': tensorboard['options']}

    stats_file_path = "%s/stats.json" % output_folder
    removed_epoch = []

    since = datetime.now()

    # Early stopping (Only enable when we are running at least 20 epoch)
    early_stopping = model.early_stopping is not None and nb_epoch > 20
    if early_stopping:
        if type(model.early_stopping['wait_first_n_epoch']) == float:
            wait_first_n_epoch = int(nb_epoch * model.early_stopping['wait_first_n_epoch'])
        else:
            wait_first_n_epoch = model.early_stopping['wait_first_n_epoch']

        if type(model.early_stopping['stop_threshold']) == float:
            stop_threshold = int(nb_epoch*model.early_stopping['stop_threshold'])
        else:
            stop_threshold = model.early_stopping['stop_threshold']

        wait_first_n_epoch += start_epoch           # Apply grace period even when continuing training
        stop_threshold = max(stop_threshold, 1)
        best_val_loss = 9999
        early_stop_counter = 0

    # TODO : Write hyperparams to tensorboard

    for epoch in range(start_epoch, start_epoch + nb_epoch):
        epoch_output_folder_path = "%s/Epoch_%.2d" % (output_folder, epoch)
        create_folder_if_necessary(epoch_output_folder_path)
        print('Epoch {}/{}'.format(epoch, start_epoch + nb_epoch - 1))
        print('-' * 10)

        epoch_time = datetime.now()
        tensorboard_per_set['writer'] = tensorboard['writers']['train']
        train_loss, train_acc, train_predictions = process_dataloader(True, device, model,
                                                                      dataloaders['train'],
                                                                      criterion, optimizer, scheduler=scheduler,
                                                                      epoch_id=epoch, tensorboard=tensorboard_per_set,
                                                                      gamma_beta_path="%s/train_gamma_beta.h5" % epoch_output_folder_path)
        epoch_train_time = datetime.now() - epoch_time

        print('\n{} Loss: {:.4f} Acc: {:.4f}'.format('Train', train_loss, train_acc))

        tensorboard_per_set['writer'] = tensorboard['writers']['val']
        val_loss, val_acc, val_predictions = process_dataloader(False, device, model,
                                                                dataloaders['val'], criterion,
                                                                epoch_id=epoch, tensorboard=tensorboard_per_set,
                                                                gamma_beta_path="%s/val_gamma_beta.h5" % epoch_output_folder_path)
        print('\n{} Loss: {:.4f} Acc: {:.4f}'.format('Val', val_loss, val_acc))

        stats = save_training_stats(stats_file_path, epoch, train_acc, train_loss, val_acc, val_loss, epoch_train_time)

        save_json(train_predictions, epoch_output_folder_path, filename="train_predictions.json")
        save_json(val_predictions, epoch_output_folder_path, filename="val_predictions.json")

        # Save training weights
        checkpoint = {
            'epoch': epoch,
            'model_state_dict': model.get_cleaned_state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'loss': train_loss,
            'rng_state': get_random_state(),
        }

        if scheduler:
            checkpoint['scheduler_state_dict'] = scheduler.state_dict()

        torch.save(checkpoint, '%s/model.pt.tar' % epoch_output_folder_path)

        sorted_stats = sort_stats(stats)

        if nb_epoch_to_keep is not None:
            # FIXME : Probably not the most efficient way to do this
            epoch_to_remove = sorted_stats[nb_epoch_to_keep:]

            for epoch_stat in epoch_to_remove:
                if epoch_stat['epoch'] not in removed_epoch:
                    removed_epoch.append(epoch_stat['epoch'])

                    shutil.rmtree("%s/%s" % (output_folder, epoch_stat['epoch']))

        print("Epoch took %s" % str(datetime.now() - epoch_time))

        # Create a symlink to best epoch output folder
        best_epoch = sorted_stats[0]
        print("Best Epoch is {} with Loss: {} Acc: {}".format(best_epoch['epoch'],
                                                              best_epoch['val_loss'],
                                                              best_epoch['val_acc']))
        best_epoch_symlink_path = '%s/best' % output_folder
        subprocess.run("ln -snf %s %s" % (best_epoch['epoch'], best_epoch_symlink_path), shell=True)

        # Early Stopping
        if early_stopping:
            if val_loss < best_val_loss - model.early_stopping['min_step']:
                best_val_loss = val_loss
                early_stop_counter = 0
            elif epoch > wait_first_n_epoch:
                early_stop_counter += 1
                print("Early Stopping count : %d/%d" % (early_stop_counter, stop_threshold))

                if early_stop_counter >= stop_threshold:
                    print("Early Stopping at epoch %d on %d" % (epoch, nb_epoch))
                    break

        print()

    time_elapsed = datetime.now() - since
    print(f'Training complete in {time_elapsed}')
    print(f'Best val Acc: {best_epoch["val_acc"]}')

    # TODO : load best model weights ?
    #model.load_state_dict(best_model_state)
    return model

# TODO : Interactive mode, One game at a time

def set_inference(device, model, dataloader, criterion, output_folder, save_gamma_beta=True):
    set_type = dataloader.dataset.set

    if save_gamma_beta:
        gamma_beta_path = '%s/%s_gamma_beta.h5' % (output_folder, set_type)
    else:
        gamma_beta_path = None

    _, acc, predictions = process_dataloader(False, device, model, dataloader, criterion=criterion, gamma_beta_path=None)#gamma_beta_path)

    save_json(predictions, output_folder, filename='%s_predictions.json' % set_type)

    print("%s Accuracy : %.5f" % (set_type, acc))

