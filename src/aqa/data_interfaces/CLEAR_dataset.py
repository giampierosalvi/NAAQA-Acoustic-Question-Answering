import json
import math
import random
import collections
import numpy as np
from multiprocessing import Semaphore
import os

from src.aqa.data_interfaces.CLEAR_image_loader import CLEARImage

class Game(object):
    def __init__(self, id, image, question, answer, question_family_index):
        self.id = id
        self.image = image
        self.question = question
        self.answer = answer
        self.question_family_index = question_family_index

    def __str__(self):
        return "[#q:{}, #p:{}] {} - {} ({})".format(self.id, self.image.id, self.question, self.answer, self.question_family_index)


class CLEARDataset(object):
    """Loads the CLEAR dataset."""

    def __init__(self, folder, which_set, image_builder, tokenizer):

        question_file_path = '{}/questions/CLEAR_{}_questions.json'.format(folder, which_set)

        self.tokenizer = tokenizer
        self.games = []
        self.question_family_index = collections.Counter()
        self.answer_counter = collections.Counter()

        with open(question_file_path) as question_file:
            data = json.load(question_file)
            info = data["info"]
            samples = data["questions"]

            assert info["set_type"] == which_set

            for sample in samples:

                question_id = int(sample["question_index"])
                question = self.tokenizer.encode_question(sample["question"])

                answer = sample.get("answer", None)  # None for test set
                if answer is not None:
                    answer = self.tokenizer.encode_answer(answer)

                question_family_index = sample.get("question_family_index", -1)  # -1 for test set      # FIXME : What is this even used for ?

                image_id = int(sample["scene_index"])
                image_filename = sample["scene_filename"].replace('.wav', ".png")       # The clear dataset specify the filename to the scene wav file

                self.games.append(Game(id=question_id,
                                  image=CLEARImage(image_id, image_filename, image_builder, which_set),
                                  question=question,
                                  answer=answer,
                                  question_family_index=question_family_index))

                self.question_family_index[question_family_index] += 1
                self.answer_counter[answer] += 1

        print("Successfully Loaded CLEAR v{} ({}) - {} games loaded.".format(info["version"], which_set, len(self.games)))

    def get_data(self, indices=[]):
        if len(indices) > 0:
            return [self.games[i] for i in indices]
        else:
            return self.games

    def __len__(self):
        return len(self.games)

class CLEARBatchifier(object):
    """Provides an generic multithreaded iterator over the dataset."""

    def __init__(self, dataset, batch_size, pool, tokenizer,
                 shuffle= True, no_semaphore= 20):

        # Filtered games
        games = dataset.get_data()
        self.tokenizer = tokenizer

        if shuffle:
            random.shuffle(games)

        self.n_examples = len(games)
        self.batch_size = batch_size

        self.n_batches = int(math.ceil(1. * self.n_examples / self.batch_size))

        # Split batches
        i = 0
        batches = []

        while i <= len(games):
            end = min(i + batch_size, len(games))
            batches.append(games[i:end])
            i += batch_size

        # Multi_proc
        def create_semaphore_iterator(obj_list, semaphores):
            for obj in obj_list:
                semaphores.acquire()
                yield obj

        self.semaphores = Semaphore(no_semaphore)
        batches = create_semaphore_iterator(batches, self.semaphores)
        self.process_iterator = pool.imap(self.load_batch, batches)

    def load_batch(self, games):

        batch = collections.defaultdict(list)
        batch_size = len(games)

        assert batch_size > 0

        for i, game in enumerate(games):

            #batch["raw"].append(game)

            batch['question'].append(game.question)
            batch['answer'].append(game.answer)

            # retrieve the image source type
            img = game.image.get_image()    # FIXME : This is the thing that should be parallelize in a CPU Pool..
            if "image" not in batch: # initialize an empty array for better memory consumption
                batch["image"] = np.zeros((batch_size,) + img.shape, dtype=np.float32)
            batch["image"][i] = img

        batch['question'], batch['seq_length'] = self.tokenizer.pad_tokens(batch['question'])

        return batch

    def __len__(self):
        return self.n_batches

    def __iter__(self):
        return self

    def __next__(self):
        self.semaphores.release()
        return self.process_iterator.next()

    # trick for python 2.X
    def next(self):
        return self.__next__()
