from collections import defaultdict
import argparse, time, math, json, os
import pandas as pd

import torch
import torch.nn as nn
from torch.autograd import Variable

import data
import model

parser = argparse.ArgumentParser(description='PyTorch RNN/LSTM Language Model')
parser.add_argument('--data', type=str, default='./data/penn', help='location of the data corpus')
parser.add_argument('--model', type=str, default='LSTM', help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU)')
parser.add_argument('--emsize', type=int, default=200, help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=200, help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=2, help='number of layers')
parser.add_argument('--lr', type=float, default=20, help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25, help='gradient clipping')
parser.add_argument('--epochs', type=int, default=40, help='upper epoch limit')
parser.add_argument('--batch-size', type=int, default=20, metavar='N', help='batch size')
parser.add_argument('--bptt', type=int, default=35, help='sequence length')
parser.add_argument('--dropout', type=float, default=0.2, help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--tied', action='store_true', help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111, help='random seed')
parser.add_argument('--cuda', action='store_true', help='use CUDA')
parser.add_argument('--log-interval', type=int, default=200, metavar='N', help='report interval')
parser.add_argument('--result-path', type=str,  default='result', help='directory to save the results')
parser.add_argument('--LAMBDA', default=0., type=float, help='constant to multiply with center loss (default %(default)s)')
parser.add_argument('--ALPHA', default=0.5, type=float, help='learning rate to update embedding centroids (default %(default)s)')
args = parser.parse_args()

result_path = args.result_path
assert not(os.path.exists(result_path)), "result dir already exists!"
os.makedirs(result_path)
config_str = json.dumps(vars(args))
config_file = os.path.join(result_path, 'config')
config_file_object = open(config_file, 'w')
config_file_object.write(config_str)
config_file_object.close()

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")
    else:
        torch.cuda.manual_seed(args.seed)

###############################################################################
# Load data
###############################################################################

corpus = data.Corpus(args.data)

def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    if args.cuda:
        data = data.cuda()
    return data

eval_batch_size = 10
train_data = batchify(corpus.train, args.batch_size)
val_data = batchify(corpus.valid, eval_batch_size)
test_data = batchify(corpus.test, eval_batch_size)

###############################################################################
# Build the model
###############################################################################

ntokens = len(corpus.dictionary)
model = model.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, 
          dropout=args.dropout, tie_weights=args.tied, ALPHA=args.ALPHA)
if args.cuda:
    model.cuda()
center_loss_factor = args.LAMBDA

###############################################################################
# Training code
###############################################################################

def repackage_hidden(h):
    """Wraps hidden states in new Variables, to detach them from their history."""
    if type(h) == Variable:
        return Variable(h.data)
    else:
        return tuple(repackage_hidden(v) for v in h)

def get_batch(source, i, evaluation=False):
    seq_len = min(args.bptt, len(source) - 1 - i)
    data = Variable(source[i:i+seq_len], volatile=evaluation)
    target = Variable(source[i+1:i+1+seq_len].view(-1))
    return data, target


def evaluate(data_source):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(eval_batch_size)
    criterion = nn.CrossEntropyLoss()
    for i in range(0, data_source.size(0) - 1, args.bptt):
        data, targets = get_batch(data_source, i, evaluation=True)
        output, hidden = model(data, hidden)
        output_flat = output.view(-1, ntokens)
        total_loss += len(data) * criterion(output_flat, targets).data
        hidden = repackage_hidden(hidden)
    return total_loss[0] / len(data_source)

model_path = os.path.join(result_path, 'model.pt')

train_metrics = defaultdict(list)
eval_metrics = defaultdict(list)
test_metrics = defaultdict(list)
def train():
    # Turn on training mode which enables dropout.
    model.train()
    total_loss = 0
    start_time = time.time()
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(args.batch_size)
    criterion = nn.CrossEntropyLoss()
    for batch, i in enumerate(range(0, train_data.size(0) - 1, args.bptt)):
        data, targets = get_batch(train_data, i)
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        hidden = repackage_hidden(hidden)
        model.zero_grad()
        logits, hidden = model(data, hidden)
        loss_values = model.calculate_loss_values(logits, targets)
        loss_values_data = tuple(map(lambda x: x.data[0], loss_values))
        cross_entropy_val, center_loss_val = loss_values_data
        cross_entropy_loss = loss_values[0]
        center_loss = loss_values[1]
        if center_loss_factor > 0:
            train_loss = cross_entropy_loss + center_loss_factor*center_loss
        else:
            train_loss = cross_entropy_loss
        train_loss.backward()

        train_loss_val = train_loss.data[0]
        perplexity_val = math.exp(cross_entropy_val)

        train_metrics['train_loss'].append(train_loss_val)
        train_metrics['center_loss'].append(center_loss_val)
        train_metrics['cross_entropy'].append(cross_entropy_val)
        train_metrics['perplexity'].append(perplexity_val)

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
        for name,p in model.named_parameters():
            if p.requires_grad:
                p.data.add_(-lr, p.grad.data)

        total_loss += cross_entropy_val

        if batch % args.log_interval == 0 and batch > 0:
            cur_loss = total_loss / args.log_interval
            elapsed = time.time() - start_time
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                    'ce loss {:5.2f} | ppl {:8.2f}'.format(
                epoch, batch, len(train_data) // args.bptt, lr,
                elapsed * 1000 / args.log_interval, cur_loss, math.exp(cur_loss)))
            print('| train loss: {:.3f} | center loss: {:.3f} | cross entropy: {:.3f} | '
                  'perplexity: {:.3f}'.format(train_loss_val, center_loss_val, 
                  cross_entropy_val, perplexity_val))

            total_loss = 0
            start_time = time.time()

# Loop over epochs.
lr = args.lr
best_val_loss = None

# At any point you can hit Ctrl + C to break out of training early.
try:
    for epoch in range(1, args.epochs+1):
        epoch_start_time = time.time()
        train()
        val_loss = evaluate(val_data)
        val_perplexity = math.exp(val_loss)
        eval_metrics['cross_entropy'].append(val_loss)
        eval_metrics['perplexity'].append(val_perplexity)
        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | '
                'valid ppl {:8.2f}'.format(epoch, (time.time() - epoch_start_time),
                                           val_loss, val_perplexity))
        print('-' * 89)
        # Save the model if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            with open(model_path, 'wb') as f:
                torch.save(model, f)
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0

        pd_train_metrics = pd.DataFrame(train_metrics)
        pd_train_metrics.to_csv(os.path.join(result_path, 'train_metrics.csv'))
        pd_eval_metrics = pd.DataFrame(eval_metrics)
        pd_eval_metrics.to_csv(os.path.join(result_path, 'eval_metrics.csv'))

except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')

# Load the best saved model.
with open(model_path, 'rb') as f:
    model = torch.load(f)

# Run on test data.
test_loss = evaluate(test_data)
test_perplexity = math.exp(test_loss)
print('=' * 89)
print('| End of training | test loss {:5.2f} | test ppl {:8.2f}'.format(
    test_loss, test_perplexity))
print('=' * 89)

test_metrics['cross_entropy'].append(test_loss)
test_metrics['perplexity'].append(test_perplexity)
pd_test_metrics = pd.DataFrame(test_metrics)
pd_test_metrics.to_csv(os.path.join(result_path, 'test_metrics.csv'))
