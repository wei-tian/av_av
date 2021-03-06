import os, sys, pdb
sys.path.append(os.path.dirname(sys.path[0]))

import torch
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter
import numpy as np
import pandas as pd
import random
from sklearn.metrics import accuracy_score, f1_score, confusion_matrix, precision_score, recall_score, roc_auc_score, roc_curve
from sklearn import preprocessing
from matplotlib import pyplot as plt

from core.relation_extractor import Relations
from argparse import ArgumentParser
from pathlib import Path
from tqdm import tqdm
from core.mrgcn import *
from torch_geometric.data import Data, DataLoader, DataListLoader
from sklearn.utils.class_weight import compute_class_weight
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
from sklearn.utils import resample
import pickle as pkl
from sklearn.model_selection import train_test_split

from collections import Counter
class Config:
    '''Argument Parser for script to train scenegraphs.'''
    def __init__(self, args):
        self.parser = ArgumentParser(description='The parameters for training the scene graph using GCN.')
        self.parser.add_argument('--cache_path', type=str, default="../script/image_dataset.pkl", help="Path to the cache file.")
        self.parser.add_argument('--transfer_path', type=str, default="", help="Path to the transfer file.")
        self.parser.add_argument('--model_load_path', type=str, default="./model/model_best_val_loss_.vec.pt", help="Path to load cached model file.")
        self.parser.add_argument('--model_save_path', type=str, default="./model/model_best_val_loss_.vec.pt", help="Path to save model file.")
        self.parser.add_argument('--split_ratio', type=float, default=0.3, help="Ratio of dataset withheld for testing.")
        self.parser.add_argument('--downsample', type=lambda x: (str(x).lower() == 'true'), default=False, help='Set to true to downsample dataset.')
        self.parser.add_argument('--learning_rate', default=0.0001, type=float, help='The initial learning rate for GCN.')
        self.parser.add_argument('--seed', type=int, default=random.randint(0,2**32), help='Random seed.')
        self.parser.add_argument('--epochs', type=int, default=200, help='Number of epochs to train.')
        self.parser.add_argument('--activation', type=str, default='relu', help='Activation function to use, options: [relu, leaky_relu].')
        self.parser.add_argument('--weight_decay', type=float, default=5e-4, help='Weight decay (L2 loss on parameters).')
        self.parser.add_argument('--dropout', type=float, default=0.25, help='Dropout rate (1 - keep probability).')
        self.parser.add_argument('--nclass', type=int, default=2, help="The number of classes for dynamic graph classification (currently only supports 2).")
        self.parser.add_argument('--batch_size', type=int, default=32, help='Number of graphs in a batch.')
        self.parser.add_argument('--device', type=str, default="cpu", help='The device on which models are run, options: [cuda, cpu].')
        self.parser.add_argument('--test_step', type=int, default=10, help='Number of training epochs before testing the model.')
        self.parser.add_argument('--model', type=str, default="mrgcn", help="Model to be used intrinsically. options: [mrgcn, mrgin]")
        self.parser.add_argument('--conv_type', type=str, default="FastRGCNConv", help="type of RGCNConv to use [RGCNConv, FastRGCNConv].")
        self.parser.add_argument('--num_layers', type=int, default=3, help="Number of layers in the network.")
        self.parser.add_argument('--hidden_dim', type=int, default=32, help="Hidden dimension in RGCN.")
        self.parser.add_argument('--layer_spec', type=str, default=None, help="manually specify the size of each layer in format l1,l2,l3 (no spaces).")
        self.parser.add_argument('--pooling_type', type=str, default="sagpool", help="Graph pooling type, options: [sagpool, topk, None].")
        self.parser.add_argument('--pooling_ratio', type=float, default=0.5, help="Graph pooling ratio.")        
        self.parser.add_argument('--readout_type', type=str, default="mean", help="Readout type, options: [max, mean, add].")
        self.parser.add_argument('--temporal_type', type=str, default="lstm_attn", help="Temporal type, options: [mean, lstm_last, lstm_sum, lstm_attn].")
        self.parser.add_argument('--lstm_input_dim', type=int, default=50, help="LSTM input dimensions.")
        self.parser.add_argument('--lstm_output_dim', type=int, default=20, help="LSTM output dimensions.")
        self.parser.add_argument('--stats_path', type=str, default="best_stats.csv", help="path to save best test statistics.")

        args_parsed = self.parser.parse_args(args)
        
        for arg_name in vars(args_parsed):
            self.__dict__[arg_name] = getattr(args_parsed, arg_name)

        self.cache_path = Path(self.cache_path).resolve()
        if self.transfer_path != "":
            self.transfer_path = Path(self.transfer_path).resolve()
        else:
            self.transfer_path = None
        self.stats_path = Path(self.stats_path.strip()).resolve()

def build_scenegraph_dataset(cache_path, train_to_test_ratio=0.3, downsample=False, seed=0, transfer_path=None):
    dataset_file = open(cache_path, "rb")
    scenegraphs_sequence, feature_list = pkl.load(dataset_file)

    if transfer_path == None:

        class_0 = []
        class_1 = []

        for g in scenegraphs_sequence:
            if g['label'] == 0:
                class_0.append(g)
            elif g['label'] == 1:
                class_1.append(g)
            
        y_0 = [0]*len(class_0)
        y_1 = [1]*len(class_1)

        min_number = min(len(class_0), len(class_1))
        if downsample:
            modified_class_0, modified_y_0 = resample(class_0, y_0, n_samples=min_number)
        else:
            modified_class_0, modified_y_0 = class_0, y_0
            
        train, test, train_y, test_y = train_test_split(modified_class_0+class_1, modified_y_0+y_1, test_size=train_to_test_ratio, shuffle=True, stratify=modified_y_0+y_1, random_state=seed)

        return train, test, feature_list

    else: 

        test, _ = pkl.load(open(transfer_path, "rb"))

        return scenegraphs_sequence, test, feature_list 

class DynKGTrainer:

    def __init__(self, args):
        self.config = Config(args)
        self.args = args
        np.random.seed(self.config.seed)
        torch.manual_seed(self.config.seed)

        if not self.config.cache_path.exists():
            raise Exception("The cache file does not exist.")    

        self.training_data, self.testing_data, self.feature_list = build_scenegraph_dataset(self.config.cache_path, self.config.split_ratio, downsample=self.config.downsample, seed=self.config.seed, transfer_path=self.config.transfer_path)
        self.training_labels = [data['label'] for data in self.training_data]
        self.testing_labels = [data['label'] for data in self.testing_data]
        self.class_weights = torch.from_numpy(compute_class_weight('balanced', np.unique(self.training_labels), self.training_labels))
        print("Number of Sequences Included: ", len(self.training_data))
        print("Num Labels in Each Class: " + str(np.unique(self.training_labels, return_counts=True)[1]) + ", Class Weights: " + str(self.class_weights))

        self.summary_writer = SummaryWriter()

        self.best_val_loss = 99999
        self.best_epoch = 0
        self.best_val_acc = 0
        self.best_val_confusion = []

    def build_model(self):
        self.config.num_features = len(self.feature_list)
        self.config.num_relations = max([r.value for r in Relations])+1
        if self.config.model == "mrgcn":
            self.model = MRGCN(self.config).to(self.config.device)
        elif self.config.model == "mrgin":
            self.model = MRGIN(self.config).to(self.config.device)
        else:
            raise Exception("model selection is invalid: " + self.config.model)

        self.optimizer = optim.Adam(self.model.parameters(), lr=self.config.learning_rate, weight_decay=self.config.weight_decay)
        if self.class_weights.shape[0] < 2:
            self.loss_func = nn.CrossEntropyLoss()
        else:    
           self.loss_func = nn.CrossEntropyLoss(weight=self.class_weights.float().to(self.config.device))

    def train(self):
        
        tqdm_bar = tqdm(range(self.config.epochs))

        for epoch_idx in tqdm_bar: # iterate through epoch   
            acc_loss_train = 0
            
            self.sequence_loader = DataListLoader(self.training_data, batch_size=self.config.batch_size)

            for data_list in self.sequence_loader: # iterate through scenegraphs
                self.model.train()
                self.optimizer.zero_grad()
                
                labels = torch.empty(0).long().to(self.config.device)
                outputs = torch.empty(0,2).to(self.config.device)
                for sequence in data_list: # iterate through sequences

                    data, label = sequence['sequence'], sequence['label']
                    graph_list = [Data(x=g['node_features'], edge_index=g['edge_index'], edge_attr=g['edge_attr']) for g in data]
                
                    # data is a sequence that consists of serveral graphs 
                    self.train_loader = DataLoader(graph_list, batch_size=len(graph_list))
                    sequence = next(iter(self.train_loader)).to(self.config.device)

                    output, _ = self.model.forward(sequence.x, sequence.edge_index, sequence.edge_attr, sequence.batch)
                    outputs = torch.cat([outputs, output.view(-1, 2)], dim=0)
                    labels  = torch.cat([labels, torch.LongTensor([label]).to(self.config.device)], dim=0)
                

                loss_train = self.loss_func(outputs, labels)
                loss_train.backward()
                acc_loss_train += loss_train.detach().cpu().item() * len(data_list)
                self.optimizer.step()

            acc_loss_train /= len(self.training_data)
            tqdm_bar.set_description('Epoch: {:04d}, loss_train: {:.4f}'.format(epoch_idx, acc_loss_train))
            
            if epoch_idx % self.config.test_step == 0:
                _, _, metrics, _ = self.evaluate(epoch_idx)
                self.summary_writer.add_scalar('Acc_Loss/train', metrics['train']['loss'], epoch_idx)
                self.summary_writer.add_scalar('Acc_Loss/train_acc', metrics['train']['acc'], epoch_idx)
                self.summary_writer.add_scalar('F1/train', metrics['train']['f1'], epoch_idx)
                # self.summary_writer.add_scalar('Confusion/train', metrics['train']['confusion'], epoch_idx)
                self.summary_writer.add_scalar('Precision/train', metrics['train']['precision'], epoch_idx)
                self.summary_writer.add_scalar('Recall/train', metrics['train']['recall'], epoch_idx)
                self.summary_writer.add_scalar('Auc/train', metrics['train']['auc'], epoch_idx)

                self.summary_writer.add_scalar('Acc_Loss/test', metrics['test']['loss'], epoch_idx)
                self.summary_writer.add_scalar('Acc_Loss/test_acc', metrics['test']['acc'], epoch_idx)
                self.summary_writer.add_scalar('F1/test', metrics['test']['f1'], epoch_idx)
                # self.summary_writer.add_scalar('Confusion/test', metrics['test']['confusion'], epoch_idx)
                self.summary_writer.add_scalar('Precision/test', metrics['test']['precision'], epoch_idx)
                self.summary_writer.add_scalar('Recall/test', metrics['test']['recall'], epoch_idx)
                self.summary_writer.add_scalar('Auc/test', metrics['test']['auc'], epoch_idx)

    def inference(self, testing_data, testing_labels):
        labels = []
        outputs = []
        acc_loss_test = 0
        folder_names = []
        attns_weights = []
        node_attns = []

        with torch.no_grad():
            for i in range(len(testing_data)): # iterate through scenegraphs
                data, label = testing_data[i]['sequence'], testing_labels[i]
                
                data_list = [Data(x=g['node_features'], edge_index=g['edge_index'], edge_attr=g['edge_attr']) for g in data]

                self.test_loader = DataLoader(data_list, batch_size=len(data_list))
                sequence = next(iter(self.test_loader)).to(self.config.device)

                self.model.eval()
                output, attns = self.model.forward(sequence.x, sequence.edge_index, sequence.edge_attr, sequence.batch)
                
                loss_test = self.loss_func(output.view(-1, 2), torch.LongTensor([label]).to(self.config.device))
                
                acc_loss_test += loss_test.detach().cpu().item()

                outputs.append(output.detach().cpu().numpy().tolist())
                labels.append(label)
                folder_names.append(testing_data[i]['folder_name'])
                if 'lstm_attn_weights' in attns:
                    attns_weights.append(attns['lstm_attn_weights'].squeeze().detach().cpu().numpy().tolist())
                if 'pool_score' in attns:
                    node_attn = {}
                    node_attn["original_batch"] = sequence.batch.detach().cpu().numpy().tolist()
                    node_attn["pool_perm"] = attns['pool_perm'].detach().cpu().numpy().tolist()
                    node_attn["pool_batch"] = attns['batch'].detach().cpu().numpy().tolist()
                    node_attn["pool_score"] = attns['pool_score'].detach().cpu().numpy().tolist()
                    node_attns.append(node_attn)

        return outputs, labels, folder_names, acc_loss_test/len(testing_data), attns_weights, node_attns
    
    def evaluate(self, current_epoch=None):
        metrics = {}

        outputs_train, labels_train, folder_names_train, acc_loss_train, attns_train, node_attns_train = self.inference(self.training_data, self.training_labels)
        metrics['train'] = get_metrics(outputs_train, labels_train)
        metrics['train']['loss'] = acc_loss_train

        outputs_test, labels_test, folder_names_test, acc_loss_test, attns_test, node_attns_test = self.inference(self.testing_data, self.testing_labels)
        metrics['test'] = get_metrics(outputs_test, labels_test)
        metrics['test']['loss'] = acc_loss_test

        print("\ntrain loss: " + str(acc_loss_train) + ", acc:", metrics['train']['acc'], metrics['train']['confusion'], metrics['train']['auc'], \
              "\ntest loss: " +  str(acc_loss_test) + ", acc:",  metrics['test']['acc'],  metrics['test']['confusion'], metrics['test']['auc'])

        #automatically save the model and metrics with the lowest validation loss
        if acc_loss_test < self.best_val_loss:
            self.best_val_loss = acc_loss_test
            self.best_epoch = current_epoch if current_epoch != None else self.config.epochs

            best_metrics = {}
            best_metrics['args'] = str(self.args)
            best_metrics['epoch'] = self.best_epoch
            best_metrics['val loss'] = acc_loss_test
            best_metrics['val acc'] = metrics['test']['acc']
            best_metrics['val conf'] = metrics['test']['confusion']
            best_metrics['val auc'] = metrics['test']['auc']
            best_metrics['val precision'] = metrics['test']['precision']
            best_metrics['val recall'] = metrics['test']['recall']
            best_metrics['train loss'] = acc_loss_train
            best_metrics['train acc'] = metrics['train']['acc']
            best_metrics['train conf'] = metrics['train']['confusion'] 
            best_metrics['train auc'] = metrics['train']['auc']
            best_metrics['train precision'] = metrics['train']['precision']
            best_metrics['train recall'] = metrics['train']['recall']
            
            

            if not self.config.stats_path.exists():
                current_stats = pd.DataFrame(best_metrics, index=[0])
                current_stats.to_csv(str(self.config.stats_path), mode='w+', header=True, index=False, columns=list(best_metrics.keys()))
            else:
                best_stats = pd.read_csv(str(self.config.stats_path), header=0)
                best_stats = best_stats.reset_index(drop=True)
                replace_row = best_stats.loc[best_stats.args == str(self.args)]
                if(replace_row.empty):
                    current_stats = pd.DataFrame(best_metrics, index=[0])
                    current_stats.to_csv(str(self.config.stats_path), mode='a', header=False, index=False, columns=list(best_metrics.keys()))
                else:
                    best_stats.iloc[replace_row.index] = pd.DataFrame(best_metrics, index=replace_row.index)
                    best_stats.to_csv(str(self.config.stats_path), mode='w', header=True,index=False, columns=list(best_metrics.keys()))

            self.save_model()

        return outputs_test, labels_test, metrics, folder_names_train

    def save_model(self):
        """Function to save the model."""
        saved_path = Path(self.config.model_save_path).resolve()
        os.makedirs(os.path.dirname(saved_path), exist_ok=True)
        torch.save(self.model.state_dict(), str(saved_path))
        with open(os.path.dirname(saved_path) + "/model_parameters.txt", "w+") as f:
            f.write(str(self.config))
            f.write('\n')
            f.write(str(' '.join(sys.argv)))

    def load_model(self):
        """Function to load the model."""
        saved_path = Path(self.config.model_load_path).resolve()
        if saved_path.exists():
            self.build_model()
            self.model.load_state_dict(torch.load(str(saved_path)))
            self.model.eval()

def get_metrics(outputs, labels):
    labels_tensor = torch.LongTensor(labels).detach()
    outputs_tensor = torch.FloatTensor(outputs).detach()
    preds = outputs_tensor.max(1)[1].type_as(labels_tensor).detach()

    metrics = {}
    metrics['acc'] = accuracy_score(labels_tensor, preds)
    metrics['f1'] = f1_score(labels_tensor, preds, average="binary")
    metrics['confusion'] = str(confusion_matrix(labels_tensor, preds)).replace('\n', ',')
    metrics['precision'] = precision_score(labels_tensor, preds, average="binary")
    metrics['recall'] = recall_score(labels_tensor, preds, average="binary")
    metrics['auc'] = get_auc(outputs_tensor, labels_tensor)
    metrics['label_distribution'] = str(np.unique(labels_tensor, return_counts=True)[1])
    
    return metrics 

#returns onehot version of labels. can specify n_classes to force onehot size.
def encode_onehot(labels, n_classes=None):
    if(n_classes):
        classes = set(range(n_classes))
    else:
        classes = set(labels)
    classes_dict = {c: np.identity(len(classes))[i, :] for i, c in
                    enumerate(classes)}
    labels_onehot = np.array(list(map(classes_dict.get, labels)),
                             dtype=np.int32)
    return labels_onehot

#~~~~~~~~~~Scoring Metrics~~~~~~~~~~
#note: these scoring metrics only work properly for binary classification use cases (graph classification, dyngraph classification) 
def get_auc(outputs, labels):
    try:    
        labels = encode_onehot(labels.numpy().tolist(), 2) #binary labels
        auc = roc_auc_score(labels, outputs.numpy(), average="micro")
    except ValueError as err: 
        print("error calculating AUC: ", err)
        auc = 0.0
    return auc

#NOTE: ROC curve is only generated for positive class (risky label) confidence values 
#render parameter determines if the figure is actually generated. If false, it saves the values to a csv file.
def get_roc_curve(outputs, labels, render=False):
    risk_scores = []
    outputs = preprocessing.normalize(outputs.numpy(), axis=0)
    for i in outputs:
        risk_scores.append(i[1])
    fpr, tpr, thresholds = roc_curve(labels.numpy(), risk_scores)
    roc = pd.DataFrame()
    roc['fpr'] = fpr
    roc['tpr'] = tpr
    roc['thresholds'] = thresholds
    roc.to_csv("ROC_data_"+task+".csv")

    if(render):
        plt.figure(figsize=(8,8))
        plt.xlim((0,1))
        plt.ylim((0,1))
        plt.ylabel("TPR")
        plt.xlabel("FPR")
        plt.title("Receiver Operating Characteristic for " + task)
        plt.plot([0,1],[0,1], linestyle='dashed')
        plt.plot(fpr,tpr, linewidth=2)
        plt.savefig("ROC_curve_"+task+".svg")