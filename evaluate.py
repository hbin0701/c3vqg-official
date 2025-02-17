"""This script is used to evaluate a model.
"""

from torchvision import transforms

import argparse
import json
import logging
import os
import progressbar
import torch

from models.iq import IQ
from utils import NLGEval
from utils import Dict2Obj
from utils import Vocabulary
from utils import get_loader
from utils import load_vocab
from utils import process_lengths
from utils import get_glove_embedding

def evaluate(vqg, data_loader, vocab, args, params):
    """Runs BLEU, METEOR, CIDEr and distinct n-gram scores.

    Args:
            vqg: question generation model.
        data_loader: Iterator for the data.
        args: ArgumentParser object.
        params: ArgumentParser object.

    Returns:
        A float value of average loss.
    """
    vqg.eval()
    nlge = NLGEval(no_glove=True, no_skipthoughts=True)
    preds = []
    gts = []
    bar = progressbar.ProgressBar(maxval=len(data_loader))
    for iterations, (images, questions, answers,
                     categories, _) in enumerate(data_loader):

        # Set mini-batch dataset
        if torch.cuda.is_available():
            images = images.cuda()
            answers = answers.cuda()
            categories = categories.cuda()
        alengths = process_lengths(answers)

        # Predict.
        if args.from_answer:
            outputs = vqg.predict_from_answer(images, answers, alengths)
        else:
            outputs = vqg.predict_from_category(images, categories)
        for i in range(images.size(0)):
            output = vocab.tokens_to_words(outputs[i])
            preds.append(output)

            question = vocab.tokens_to_words(questions[i])
            gts.append(question)
        bar.update(iterations)
        
    print('='*80)
    print('GROUND TRUTH')
    print(gts[:args.num_show])
    print('-'*80)
    print('PREDICTIONS')
    print(preds[:args.num_show])
    print('='*80)
    scores = nlge.compute_metrics(ref_list=[gts], hyp_list=preds)
    return scores, gts, preds


def main(args):
    """Loads the model and then calls evaluate().

    Args:
        args: Instance of ArgumentParser.
    """

    # Load the arguments.
    model_dir = os.path.dirname(args.model_path)
    params = Dict2Obj(json.load(
            open(os.path.join(model_dir, "args.json"), "r")))
    print(params)
    # Config logging
    log_format = '%(levelname)-8s %(message)s'
    logfile = os.path.join(model_dir, 'eval.log')
    logging.basicConfig(filename=logfile, level=logging.INFO, format=log_format)
    logging.getLogger().addHandler(logging.StreamHandler())
    logging.info(json.dumps(args.__dict__))

    # Image preprocessing
    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225])])

    # Load vocabulary wrapper.
    vocab = load_vocab(args.vocab_path)

    # Build data loader
    logging.info("Building data loader...")

    # Load GloVe embedding.
    if params.use_glove:
        embedding = get_glove_embedding(params.embedding_name,
                                        params.hidden_size,
                                        vocab)
    else:
        embedding = None

    # Build data loader
    logging.info("Building data loader...")
    data_loader = get_loader(args.dataset, transform,
                             args.batch_size, shuffle=False,
                             num_workers=args.num_workers,
                             max_examples=args.max_examples)
    logging.info("Done")

    # Build the models
    logging.info('Creating IQ model...')
    vqg = IQ(len(vocab), params.max_length, params.hidden_size,
             params.num_categories,
             vocab(vocab.SYM_SOQ), vocab(vocab.SYM_EOS),
             num_layers=params.num_layers,
             rnn_cell=params.rnn_cell,
             dropout_p=params.dropout_p,
             input_dropout_p=params.input_dropout_p,
             encoder_max_len=params.encoder_max_len,
             embedding=embedding,
             num_att_layers=params.num_att_layers,
             z_size=params.z_size,
             z_img=params.z_img, z_category=params.z_category,               
             no_image_recon=params.no_image_recon,
             no_category_space=params.no_category_space, bayes=params.bayes)
    logging.info("Done")

    logging.info("Loading model.")
    vqg.load_state_dict(torch.load(args.model_path))

    # Setup GPUs.
    if torch.cuda.is_available():
        logging.info("Using available GPU...")
        vqg.cuda()

    scores, gts, preds = evaluate(vqg, data_loader, vocab, args, params)

    # Print and save the scores.
    print(scores)
    with open(os.path.join(model_dir, args.results_path), 'w') as results_file:
        json.dump(scores, results_file)
    with open(os.path.join(model_dir, args.preds_path), 'w') as preds_file:
        json.dump(preds, preds_file)
    with open(os.path.join(model_dir, args.gts_path), 'w') as gts_file:
        json.dump(gts, gts_file)


if __name__ == '__main__':
    # print(params)
    parser = argparse.ArgumentParser()

    # Session parameters.
    parser.add_argument('--model-path', type=str, default='weights_saved_prev/part1_2_final_100k/vqg-tf-10.pkl',
                        help='Path for loading trained models')
    parser.add_argument('--results-path', type=str, default='results.json',
                        help='Path for saving results.')
    parser.add_argument('--preds-path', type=str, default='preds.json',
                        help='Path for saving predictions.')
    parser.add_argument('--gts-path', type=str, default='gts.json',
                        help='Path for saving ground truth.')
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--num-workers', type=int, default=8)
    parser.add_argument('--seed', type=int, default=1234)
    parser.add_argument('--max-examples', type=int, default=None,
                        help='When set, only evalutes that many data points.')
    parser.add_argument('--num-show', type=int, default=10,
                        help='Number of predictions to print.')
    parser.add_argument('--from-answer', action='store_true', default=False,
                        help='When set, only evalutes iq model with answers;'
                        ' otherwise it tests iq with answer types.')
    parser.add_argument('--vocab-path', type=str,
                        default='../iq/data/processed/vocab_iq.json',
                        help='Path for vocabulary wrapper.')

    # # Model parameters
    parser.add_argument('--rnn-cell', type=str, default='LSTM',
                        help='Type of rnn cell (GRU, RNN or LSTM).')
    parser.add_argument('--hidden_size', type=int, default=512,
                        help='Dimension of lstm hidden states.')
    parser.add_argument('--num-layers', type=int, default=1,
                        help='Number of layers in lstm.')
    parser.add_argument('--max-length', type=int, default=20,
                        help='Maximum sequence length for outputs.')
    parser.add_argument('--encoder-max-len', type=int, default=8,
                        help='Maximum sequence length for inputs.')
    parser.add_argument('--bidirectional', action='store_true', default=False,
                        help='Boolean whether the RNN is bidirectional.')
    parser.add_argument('--use-glove', action='store_true',
                        help='Whether to use GloVe embeddings.')
    parser.add_argument('--embedding-name', type=str, default='6B',
                        help='Name of the GloVe embedding to use.')
    parser.add_argument('--num-categories', type=int, default=16,
                        help='Number of answer types we use.')
    parser.add_argument('--dropout-p', type=float, default=0.2,
                        help='Dropout applied to the RNN model.')
    parser.add_argument('--input-dropout-p', type=float, default=0.2,
                        help='Dropout applied to inputs of the RNN.')
    parser.add_argument('--num-att-layers', type=int, default=2,
                        help='Number of attention layers.')
    parser.add_argument('--z_size', type=int, default=64,
                        help='Dimensions to use for hidden variational space.')
    parser.add_argument('--z_img', type=int, default=512,
                        help='Dimensions to use for encoded images.')
    parser.add_argument('--z_category', type=int, default=8,
                        help='Dimensions to use for encoded categories.')

    parser.add_argument('--num_layers', type=int, default=2,
                        help='Dimensions to use for hidden variational space.')

   parser.add_argument('--dataset', type=str,
                    default='../iq/data/processed/iq_val_dataset.hdf5',
                    help='path for train annotation json file')

    args = parser.parse_args()
    torch.cuda.manual_seed(args.seed)
    torch.manual_seed(args.seed)
    main(args)
    # Hack to disable errors for importing Vocabulary. Ignore this line.
    Vocabulary()
