from collections import defaultdict

import torch
from tqdm import tqdm


def process_predictions(dataset, predictions, ground_truths, questions_id, scenes_id, predictions_probs, images_padding):
    processed_predictions = []

    iterator = zip(predictions, ground_truths, questions_id, scenes_id, predictions_probs, images_padding)
    for prediction, ground_truth, question_id, scene_id, prediction_probs, image_padding in iterator:
        # TODO : Add is_relation

        decoded_prediction = dataset.tokenizer.decode_answer(prediction)
        decoded_ground_truth = dataset.tokenizer.decode_answer(ground_truth)
        prediction_answer_family = dataset.answer_to_family[decoded_prediction]
        ground_truth_answer_family = dataset.answer_to_family[decoded_ground_truth]
        processed_predictions.append({
            'question_id': question_id,
            'scene_id': scene_id,
            'scene_length': len(dataset.scenes[scene_id]['definition']['objects']),
            'correct': bool(prediction == ground_truth),
            'correct_answer_family': bool(prediction_answer_family == ground_truth_answer_family),
            'prediction': decoded_prediction,
            'prediction_id': prediction,
            'ground_truth': decoded_ground_truth,
            'ground_truth_id': ground_truth,
            'confidence': prediction_probs[prediction],
            'prediction_answer_family': prediction_answer_family,
            'ground_truth_answer_family': ground_truth_answer_family,
            'prediction_probs': prediction_probs,
            'image_padding': image_padding
        })

    return processed_predictions


def process_gamma_beta(processed_predictions, gamma_vectors_per_resblock, beta_vectors_per_resblock):
    processed_gamma_beta_vectors = []

    if gamma_vectors_per_resblock[0].is_cuda:
        op_to_apply = lambda x: x.cpu().numpy()
    else:
        op_to_apply = lambda x: x.numpy()

    gamma_vectors_per_resblock = [op_to_apply(v) for v in gamma_vectors_per_resblock]
    beta_vectors_per_resblock = [op_to_apply(v) for v in beta_vectors_per_resblock]

    for result_index, processed_prediction in enumerate(processed_predictions):
        question_index = processed_prediction['question_id']
        processed_gamma_beta_vector = defaultdict(lambda : {})
        for resblock_index, gamma_vectors, beta_vectors in zip(range(len(gamma_vectors_per_resblock)), gamma_vectors_per_resblock, beta_vectors_per_resblock):

            processed_gamma_beta_vector['question_index'] = question_index
            processed_gamma_beta_vector['resblock_%d' % resblock_index]['gamma_vector'] = gamma_vectors[result_index]
            processed_gamma_beta_vector['resblock_%d' % resblock_index]['beta_vector'] = beta_vectors[result_index]

            # TODO : Add more attributes ? Could simply be cross loaded via question.json

        processed_gamma_beta_vectors.append(processed_gamma_beta_vector)

    return processed_gamma_beta_vectors


def calc_dataset_stats(dataloader, device='cpu'):
    """Compute the mean, std, min, max in an online fashion
    """
    assert dataloader.dataset.is_raw_img(), "Config must be set to RAW img to calculate images stats"

    print("Calculating mean and std from dataset")

    input_channel = dataloader.dataset[0]['image'].shape[0]

    cnt = 0
    fst_moment = torch.empty(input_channel, device=device)
    snd_moment = torch.empty(input_channel, device=device)
    min_val = torch.tensor(9999999, device=device).float()
    max_val = torch.tensor(-9999999, device=device).float()

    for batched_data in tqdm(dataloader):
        images = batched_data['image'].to(device)
        b, c, h, w = images.shape
        nb_pixels = b * h * w

        batch_max_val = images.max()
        batch_min_val = images.min()

        if batch_min_val < min_val:
            min_val = batch_min_val

        if batch_max_val > max_val:
            max_val = batch_max_val

        sum_ = torch.sum(images, dim=[0, 2, 3])
        sum_of_square = torch.sum(images ** 2, dim=[0, 2, 3])
        fst_moment = (cnt * fst_moment + sum_) / (cnt + nb_pixels)
        snd_moment = (cnt * snd_moment + sum_of_square) / (cnt + nb_pixels)

        cnt += nb_pixels

    snd_moment = torch.sqrt(snd_moment - fst_moment ** 2)

    return fst_moment.tolist(), snd_moment.tolist(), min_val.item(), max_val.item()

