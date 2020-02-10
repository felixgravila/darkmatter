import datetime
import os
import pandas as pd
import h5py
import numpy as np
from multiprocessing import Pool
from multiprocessing.pool import ThreadPool
from functools import partial, reduce
from collections import deque
from IPython.core.debugger import set_trace
from tensorflow.keras.utils import Sequence
import tensorflow as tf
import tensorflow.keras.backend as kb
from tensorflow.keras.models import Sequential, Model
from tensorflow.keras.layers import Input, Activation, Add, Lambda
from tensorflow.keras.layers import Dense, MaxPooling1D, Conv1D, LSTM, GRU
from tensorflow.keras.backend import ctc_batch_cost
from tensorflow.keras.callbacks import Callback
import matplotlib.pyplot as plt
from functools import reduce
import editdistance

labelBaseMap = {
    0: "A",
    1: "C",
    2: "G",
    3: "T",
    4: "-"
}

possible_filenames = ["/mnt/sdb/taiyaki_mapped/mapped_umi16to9.hdf5",
                      "/mnt/nvme/taiyaki_aligned/mapped_umi16to9.hdf5",
                      "/Users/felix/MsC/DNA/mapped_umi16to9.hdf5"]

for filename in possible_filenames:
    if os.path.isfile(filename):
        the_filename = filename
        print(f"Using {filename}")
        break
else:
    the_filename = ""
    print("Error, no filename valid!")

RNN_LEN = 300


class PrepData(Sequence):
    def __init__(self, filename, train_validate_split=0.8, min_labels=5):
        self.filename = filename
        self.train_validate_split=train_validate_split
        self.min_labels=min_labels
        self.pos = 0
        self.test_gen_data = ([],[])
        self.last_train_gen_data = ({},{})
        self.max_label_len = 50
        with h5py.File(filename, 'r') as h5file:
            self.readIDs = list(h5file['Reads'].keys())

    def get_len(self):
        return len(self.readIDs)

    def get_max_label_len(self):
        return self.max_label_len

    def normalise(self, dac):
        dmin = min(dac)
        dmax = max(dac)
        return [(d-dmin)/(dmax-dmin) for d in dac]

    def processRead(self, readID):
        train_X = []
        train_y = []
        test_X  = []
        test_y  = []
        with h5py.File(self.filename, 'r') as h5file:
            DAC = list(self.normalise(h5file['Reads'][readID]['Dacs'][()]))
            RTS = deque(list(h5file['Reads'][readID]['Ref_to_signal'][()]))
            REF = deque(h5file['Reads'][readID]['Reference'][()])

        train_validate_split = round(len(REF)*(1-self.train_validate_split))
        curdacs  = deque( [[x] for x in DAC[RTS[0]:RTS[0]+RNN_LEN-5]], RNN_LEN )
        curdacts = RTS[0]+RNN_LEN-5
        labels  = deque([])
        labelts = deque([])

        while RTS[0] < curdacts:
            labels.append(REF.popleft())
            labelts.append(RTS.popleft())


        while curdacts+5 < RTS[-1]-RNN_LEN:
            curdacs.extend([[x] for x in DAC[curdacts:curdacts+5]])
            curdacts += 5

            while RTS[0] < curdacts:
                labels.append(REF.popleft())
                labelts.append(RTS.popleft())

            while len(labelts) > 0 and labelts[0] < curdacts - RNN_LEN:
                labels.popleft()
                labelts.popleft()

            if len(labels) > self.min_labels:
                if len(RTS) > train_validate_split:
                    train_X.append(list(curdacs))
                    train_y.append(list(labels))
                else:
                    test_X.append(list(curdacs))
                    test_y.append(list(labels))

        return train_X, train_y, test_X, test_y


    def train_gen(self, full=True):
        while self.pos < len(self.readIDs):
            print(f"Processing {self.pos}")
            train_X, train_y, test_X, test_y = self.processRead(self.readIDs[self.pos])
            self.pos += 1

            train_X = np.array(train_X) if full else np.array(train_X[:100])
            train_y = np.array(train_y) if full else np.array(train_y[:100])
            test_X  = np.array(test_X) if full else np.array(test_X[:100])
            test_y  = np.array(test_y) if full else np.array(test_y[:100])
            self.test_gen_data = (test_X, test_y)

            train_X_lens = np.array([[95] for x in train_X], dtype="float32")
            train_y_lens = np.array([[len(x)] for x in train_y], dtype="float32")
            train_y_padded = np.array([r + [5]*(self.get_max_label_len()-len(r)) for r in train_y], dtype='float32')
            X = {'the_input': train_X,
                      'the_labels': train_y_padded,
                      'input_length': train_X_lens,
                      'label_length': train_y_lens,
                      'unpadded_labels' : train_y
                      }
            y = {'ctc': np.zeros([len(train_X)])}
            self.last_train_gen_data = (X, y)
            yield (X, y)

    def test_gen(self):
        while True:
            tgd, self.test_gen_data = self.test_gen_data, ([],[])
            yield tgd


    def __len__(self):
        return len(self.readIDs)

    def __getitem__(self, idx):
        return next(self.train_gen())

prepData = PrepData(filename)



gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
  try:
    tf.config.experimental.set_virtual_device_configuration(
        gpus[0],
        [tf.config.experimental.VirtualDeviceConfiguration(memory_limit=5000)])
    logical_gpus = tf.config.experimental.list_logical_devices('GPU')
    print(len(gpus), "Physical GPUs,", len(logical_gpus), "Logical GPUs")
  except RuntimeError as e:
    print(e)


def ctc_lambda_func(args):
    y_pred, labels, input_length, label_length = args
    y_pred = y_pred[:, 5:, :]
    return kb.ctc_batch_cost(labels, y_pred, input_length, label_length)

def decode_batch(modelfunc, input_data):
    pred = modelfunc(input_data)[0]
    cur = [[np.argmax(ts) for ts in p] for p in pred]
    nodup = ["".join(list(map(lambda x: labelBaseMap[x], filter(lambda x: x!=4, reduce(lambda acc, x: acc if acc[-1] == x else acc + [x], c[5:], [4]))))) for c in cur]
    return nodup

def make_res_block(upper, block):
    res = Conv1D(256, 1,
                  padding="same",
                  name=f"res{block}-r")(upper)
    upper = Conv1D(256, 1,
                  padding="same",
                  activation="relu",
                  use_bias="false",
                  name=f"res{block}-c1")(upper)
    upper = Conv1D(256, 3,
                  padding="same",
                  activation="relu",
                  use_bias="false",
                  name=f"res{block}-c2")(upper)
    upper = Conv1D(256, 1,
                  padding="same",
                  use_bias="false",
                  name=f"res{block}-c3")(upper)
    added = Add(name=f"res{block}-add")([res, upper])
    return Activation('relu', name=f"res{block}-relu")(added)

def make_bdlstm(upper, block):
    lstm_1a = LSTM(200, return_sequences=True, name=f"blstm{block}-fwd")(upper)
    lstm_1b = LSTM(200, return_sequences=True, go_backwards=True, name=f"blstm{block}-rev")(upper)
    return Add(name=f"blstm{block}-add")([lstm_1a, lstm_1b])

input_data = Input(name="the_input", shape=(300,1), dtype="float32")

inner = make_res_block(input_data, 1)
inner = make_res_block(inner, 2)
inner = make_res_block(inner, 3)
inner = make_res_block(inner, 4)
inner = make_res_block(inner, 5)
inner = make_bdlstm(inner, 1)
inner = make_bdlstm(inner, 2)
inner = make_bdlstm(inner, 3)

inner = Dense(64, name="dense", activation="relu")(inner)
inner = Dense(5, name="dense_output")(inner)

y_pred = Activation("softmax", name="softmax")(inner)

labels = Input(name='the_labels', shape=(prepData.get_max_label_len()), dtype='float32')
input_length = Input(name='input_length', shape=(1), dtype='int64')
label_length = Input(name='label_length', shape=(1), dtype='int64')

loss_out = Lambda(ctc_lambda_func, output_shape=(1,), name='ctc')([y_pred, labels, input_length, label_length])

model = Model(inputs=[input_data], outputs=y_pred, name="chiron")

model.load_weights("models/e00538_dis478.h5")


d = next(prepData.train_gen())[0]['the_input'][:100]
model.predict(d)
