import numpy as np
import json
import torch
from torch.utils.data import Dataset, DataLoader
from sphfile import SPHFile
import scipy.io.wavfile as wavfile
import librosa
from PIL import Image

EPS = 1e-9
# This function is from DAVEnet (https://github.com/dharwath/DAVEnet-pytorch)
def preemphasis(signal,coeff=0.97):
    """perform preemphasis on the input signal.
    
    :param signal: The signal to filter.
    :param coeff: The preemphasis coefficient. 0 is none, default 0.97.
    :returns: the filtered signal.
    """    
    return np.append(signal[0],signal[1:]-coeff*signal[:-1])

class ImageAudioCaptionDataset(Dataset):
  def __init__(self, audio_root_path, image_root_path, segment_file, bbox_file, configs={}):
    self.configs = configs
    self.max_nregions = configs.get('max_num_regions', 5)
    self.max_nphones = configs.get('max_num_phones', 100)
    self.max_nframes = configs.get('max_num_frames', 1000)
    self.transform = configs.get('transform', None)
    self.audio_keys = []
    self.audio_root_path = audio_root_path
    self.segmentations = []
    self.image_keys = []
    self.image_root_path = image_root_path 
    self.bboxes = []

    # Load phone segments
    with open(segment_file, 'r') as f:
      if segment_file.split('.')[-1] == 'json':
        segment_dicts = json.load(f)
        for k in sorted(segment_dicts, key=lambda x:int(x.split('_')[-1])):
          audio_file_prefix = '_'.join('_'.join(word_segment_dict[1].split('_')[:-1]) for word_segment_dict in segment_dicts[k]['data_ids'])
          self.audio_keys.append(audio_file_prefix) # TODO
          segmentation = []
          cur_start = 0
          for word_segment_dict in segment_dicts[k]['data_ids']:
              for phone_segment in word_segment_dict[2]:
                  dur = phone_segment[2] - phone_segment[1]
                  segmentation.append([cur_start, cur_start+dur])
                  cur_start += dur
          self.segmentations.append(segmentation)
      else:
        for line in f:
          k, phn, start, end = line.strip().split()
          if len(self.audio_keys) == 0:
            self.segmentations.append(segmentation)
          elif k != self.audio_keys[-1]:
            self.audio_keys.append(k)
            self.segmentations.append([[start, end]])
          else:
            self.segmentations[-1].append([start, end])
     
    with open(bbox_file, 'r') as f:
      for line in f:
        k, c, x, y, w, h = line.strip().split() 
        # XXX self.image_keys.append('_'.join(k.split('_')[:-1]))
        if len(self.image_keys) == 0:
          self.image_keys = [k]
        elif k != self.image_keys[-1]:
          self.image_keys.append(k)
          self.bboxes.append([[x, y, w, h]]) 
        else:
          self.bboxes[-1].append([x, y, w, h]) 

  def __getitem__(self, idx):
    if torch.is_tensor(idx):
      idx = idx.tolist()

    # Extract segment-level acoustic features
    self.n_mfcc = self.configs.get('n_mfcc', 40)
    # self.order = feat_configs.get('order', 2)
    self.coeff = self.configs.get('coeff', 0.97)
    self.dct_type = self.configs.get('dct_type', 3)
    self.skip_ms = self.configs.get('skip_size', 10)
    self.window_ms = self.configs.get('window_len', 25)

    mfccs = []
    phone_boundary = np.zeros(self.max_nframes, dtype=int)
    for i_s, segment in enumerate(self.segmentations[idx]):
      start_ms, end_ms = segment
      start_frame, end_frame = int(start_ms / 10), int(end_ms / 10)
      if end_frame > self.max_nframes:
        break
      phone_boundary[start_frame] = 1
      phone_boundary[end_frame] = 1

    audio_filename = '{}.wav'.format(self.audio_keys[idx])
    try:
      sr, y_wav = wavfile.read('{}/{}'.format(self.audio_root_path, audio_filename))
    except:
      if audio_filename.split('.')[-1] == 'wav':
        audio_filename_sph = '.'.join(audio_filename.split('.')[:-1]+['WAV'])
        sph = SPHFile(self.audio_root_path + audio_filename_sph)
        sph.write_wav(self.audio_root_path + audio_filename)
      sr, y_wav = io.wavfile.read(self.audio_root_path + audio_filename)
    
    y_wav = preemphasis(y_wav, self.coeff) 
    n_fft = int(self.window_ms * sr / 1000)
    hop_length = int(self.skip_ms * sr / 1000)
    mfcc = librosa.feature.mfcc(y_wav, sr=sr, n_mfcc=self.n_mfcc, dct_type=self.dct_type, n_fft=n_fft, hop_length=hop_length)
    mfcc -= np.mean(mfcc)
    mfcc /= max(np.sqrt(np.var(mfcc)), EPS)
    nframes = min(mfcc.shape[1], self.max_nframes)
    mfcc = self.convert_to_fixed_length(mfcc)
    mfcc = mfcc.T
    mfccs.append(mfcc)
    
    # Extract visual features 
    region_mask = np.zeros(self.max_nregions, dtype=int)
    regions = []
    for i_b, bbox in enumerate(self.bboxes[idx]):
      if i_b > self.max_nregions:
        break
      region_mask[i_b] = 1
      x, y, w, h = bbox 
      x, y, w, h = int(x), int(y), np.maximum(int(w), 1), np.maximum(int(h), 1)
      image = Image.open('{}/{}.jpg'.format(self.image_root_path, '_'.join(self.image_keys[idx].split('_')[:-1]))).convert('RGB')
      if len(np.array(image).shape) == 2:
        print('Wrong shape')
        image = np.tile(np.array(image)[:, :, np.newaxis], (1, 1, 3))  
        image = Image.fromarray(image)
    
      region = image.crop(box=(x, y, x + w, y + h))
      if self.transform:
        region = self.transform(region)
      regions.append(np.asarray(region))

    return torch.tensor(mfccs), torch.tensor(regions), torch.tensor(phone_boundary), torch.tensor(region_mask) 
 
  def __len__(self):
    return len(self.audio_keys)

  def convert_to_fixed_length(self, mfcc):
    T = mfcc.shape[1] 
    pad = abs(self.max_nframes - T)
    if T < self.max_nframes:
      mfcc = np.pad(mfcc, ((0, 0), (0, pad)), 'constant', constant_values=(0))
    elif T > self.max_nframes:
      mfcc = mfcc[:, :-pad]
    return mfcc  
