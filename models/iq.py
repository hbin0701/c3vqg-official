"""Contains code for the IQ model.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.distributions.normal import Normal
from .encoder_cnn import EncoderCNN
from .encoder_rnn import EncoderRNN
from .decoder_rnn import DecoderRNN
from .mlp import MLP
from .gen_ques_rnn import genQLSTM

class IQ(nn.Module):
    """Information Maximization question generation.
    """

    def __init__(self, vocab_size, max_len, hidden_size,
                 num_categories, sos_id, eos_id,
                 num_layers=1, rnn_cell='LSTM', bidirectional=False,
                 input_dropout_p=0, dropout_p=0,
                 encoder_max_len=None, num_att_layers=1, att_ff_size=512,
                 embedding=None, z_size=64, z_img=512, z_category=4,
                 no_image_recon=False, no_category_space=False, bayes=False):
        """Constructor for IQ.
        Args:
            vocab_size: Number of words in the vocabulary.
            max_len: The maximum length of the answers we generate.
            hidden_size: Number of dimensions of RNN hidden cell.
            num_categories: The number of answer categories.
            sos_id: Vocab id for <start>.
            eos_id: Vocab id for <end>.
            num_layers: The number of layers of the RNNs.
            rnn_cell: LSTM or RNN or GRU.
            bidirectional: Whether the RNN is bidirectional.
            input_dropout_p: Dropout applied to the input question words.
            dropout_p: Dropout applied internally between RNN steps.
            encoder_max_len: Maximum length of encoder.
            num_att_layers: Number of stacked attention layers.
            att_ff_size: Dimensions of stacked attention.
            embedding (vocab_size, hidden_size): Tensor of embeddings or
                None. If None, embeddings are learned.
            z_size: Dimensions of noise epsilon.
        """
        super(IQ, self).__init__()
        self.image_recon = not no_image_recon
        self.category_space = not no_category_space
        self.num_categories = num_categories
        self.hidden_size = hidden_size
        if encoder_max_len is None:
            encoder_max_len = max_len
        self.num_layers = num_layers
        self.bayes = bayes

        # Setup image encoder.
        self.encoder_cnn = EncoderCNN(z_img)

        # Setup category encoder.
        if self.category_space:
            self.category_embedding = nn.Embedding(num_categories, 8)
            self.category_encoder = MLP(8, 8, z_category,
                               num_layers=2)

        self.question_encoder = EncoderRNN(vocab_size, max_len, hidden_size, ques_encoder=True,
                                 input_dropout_p=input_dropout_p,
                                 dropout_p=dropout_p,
                                 n_layers=num_layers,
                                 bidirectional=False,
                                 rnn_cell=rnn_cell,
                                 variable_lengths=True)

        self.q_to_c = MLP(hidden_size, num_categories, num_categories,
                               num_layers=num_layers)

        # Setup stacked attention to combine image and category features.
        if self.category_space:
            self.category_attention = MLP(z_img + z_category, att_ff_size / 4, hidden_size / 4,
                                          num_layers=num_att_layers)

        self.alpha = nn.Parameter(torch.randn(z_size))

        # Setup question decoder.
        self.t_decoder = nn.Linear(z_size, z_img)
        self.gen_decoder = MLP(z_img, att_ff_size, 2 * hidden_size,
                               num_layers=1)
        self.decoder = DecoderRNN(vocab_size, max_len, 2 * hidden_size,
                                  sos_id=sos_id,
                                  eos_id=eos_id,
                                  n_layers=num_layers,
                                  rnn_cell=rnn_cell,
                                  input_dropout_p=input_dropout_p,
                                  dropout_p=dropout_p,
                                  embedding=embedding)

        # Setup encodering to t space.
        if self.category_space:
            self.mu_category_encoder = nn.Linear(hidden_size // 4, z_size)
            self.logvar_category_encoder = nn.Linear(hidden_size // 4, z_size)

        # Setup image reconstruction.
        if self.image_recon:
            self.image_reconstructor = MLP(
                    z_size, att_ff_size, z_img,
                    num_layers=num_att_layers)

        # Setup category reconstruction.
        if self.category_space:
            self.category_reconstructor = MLP(
                   z_size, att_ff_size / 2, z_category,
                   num_layers=num_att_layers,dropout_p=0.3)
            # self.category_reconstructor = MLP(                                     # for best model trained st1+2+cl+bayes
            #        z_size, hidden_size, num_categories,
            #        num_layers=num_att_layers,dropout_p=0.3)

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        d = self.alpha.data.pow(-1)
        d = torch.nan_to_num(abs(d))
        eps = Variable((Normal(torch.zeros_like(mu).cuda(), d)).sample())
        return eps.mul(std).add_(mu) + 1e-8

    def flatten_parameters(self):
        if hasattr(self, 'decoder'):
            self.decoder.rnn.flatten_parameters()
        if hasattr(self, 'encoder'):
            self.encoder.rnn.flatten_parameters()

    def generator_parameters(self):
        params = self.parameters()
        params = filter(lambda p: p.requires_grad, params)
        return params

    def cycle_params(self):
        params = (list(self.question_encoder.parameters()) +
                list(self.q_to_c.parameters()))

        params = filter(lambda p: p.requires_grad, params)
        return params

    def info_parameters(self):
        params = (list(self.category_attention.parameters()) +
                  list(self.mu_category_encoder.parameters()) +
                  list(self.logvar_category_encoder.parameters()))

        # Reconstruction parameters.
        if self.image_recon:
            params += list(self.image_reconstructor.parameters())

        if self.category_space:
            params += list(self.category_reconstructor.parameters())

        params = filter(lambda p: p.requires_grad, params)
        return params

    def reparameterize_prev(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)

    def modify_hidden(self, func, hidden, rnn_cell):
        """Applies the function func to the hidden representation.
        This method is useful because some RNNs like LSTMs have a tuples.
        Args:
            func: A function to apply to the hidden representation.
            hidden: A RNN (or LSTM or GRU) representation.
            rnn_cell: One of RNN, LSTM or GRU.
        Returns:
            func(hidden).
        """
        if rnn_cell is nn.LSTM:
            return (func(hidden[0]), func(hidden[1]))
        return func(hidden)

    def parse_outputs_to_tokens(self, outputs):
        """Converts model outputs to tokens.
        Args:
            outputs: Model outputs.
        Returns:
            A tensor of batch_size X max_len.
        """
        # Take argmax for each timestep
        # Output is list of MAX_LEN containing BATCH_SIZE * VOCAB_SIZE.

        # BATCH_SIZE * VOCAB_SIZE -> BATCH_SIZE
        outputs = [o.max(1)[1] for o in outputs]

        outputs = torch.stack(outputs)  # Tensor(max_len, batch)
        outputs = outputs.transpose(0, 1)  # Tensor(batch, max_len)
        return outputs

    def encode_images(self, images):
        """Encodes images.
        Args:
            images: Batch of image Tensors.
        Returns:
            Batch of image features.
        """
        return self.encoder_cnn(images)

    def encode_categories(self, categories):
        """Encode categories.
        Args:
            categories: Batch of category Tensors.
        Returns:
            Batch of categories encoded into features.
        """
        embedded_categories = self.category_embedding(categories)
        encoder_hidden = self.category_encoder(embedded_categories)
        return encoder_hidden

    def encode_questions_discriminator(self, questions, qlengths):
        # print('Shape before question_encoder',questions.shape)
        _, encoder_hidden = self.question_encoder(
            questions, qlengths, None)
        # print('Shape after question_encoder',encoder_hidden.shape)
        if self.question_encoder.rnn_cell == nn.LSTM:
            encoder_hidden = encoder_hidden[0]

        encoder_hidden = encoder_hidden[-1, :, :].squeeze()
        # encoder_hidden = self.q_to_c(
        #     encoder_hidden)
        # print('Shape before q_to_c',encoder_hidden.shape)

        return encoder_hidden

    def encode_questions(self, questions, qlengths):
        # print('Shape before question_encoder',questions.shape)

        _, encoder_hidden = self.question_encoder(
            questions, qlengths, None)
        if self.question_encoder.rnn_cell == nn.LSTM:
            encoder_hidden = encoder_hidden[0]
        # print('Shape after question_encoder',encoder_hidden.shape)

        encoder_hidden = encoder_hidden[-1, :, :].squeeze()
        # print('Shape before q_to_c',encoder_hidden.shape)

        encoder_hidden = self.q_to_c(
            encoder_hidden)
        return encoder_hidden

    def encode_into_t(self, image_features, category_features):
        """Encodes the attended features into t space.
        Args:
            image_features: Batch of image features.
            category_features: Batch of category features.
            input size: image_size + category_size: 512 + 4
        Returns:
            mus and logvars of the batch.
        """

        together = torch.cat((image_features, category_features), dim=1)
        attended_hiddens = self.category_attention(together)
        mus = self.mu_category_encoder(attended_hiddens)
        logvars = self.logvar_category_encoder(attended_hiddens)

        mus = mus.clamp(min=-2, max=2)
        logvars = logvars.clamp(min=-20, max=20)

        if(self.bayes==True):
            zs = self.reparameterize(mus, logvars)
        else:
            zs = self.reparameterize_prev(mus, logvars)

        return mus, logvars, zs

    def decode_questions(self, image_features, ts,
                         questions=None, teacher_forcing_ratio=0,
                         decode_function=F.log_softmax):
        """Decodes the question from the latent space.
        Args:
            image_features: Batch of image features.
            ts: Batch of latent space representations from categories.
            questions: Batch of question Variables.
            teacher_forcing_ratio: Whether to predict with teacher forcing.
            decode_function: What to use when choosing a word from the
                distribution over the vocabulary.
        """
        batch_size = ts.size(0)
        t_hiddens = self.t_decoder(ts)

        if image_features is None:
            hiddens = t_hiddens
        else:
            hiddens = self.gen_decoder(image_features + t_hiddens)

        # Reshape encoder_hidden (NUM_LAYERS * N * HIDDEN_SIZE).
        hiddens = hiddens.view((1, batch_size, 2 * self.hidden_size))

        hiddens = hiddens.expand((self.num_layers, batch_size,
                                  2 * self.hidden_size)).contiguous()

        if self.decoder.rnn_cell is nn.LSTM:
            hiddens = (hiddens, hiddens)

        result = self.decoder(inputs=questions,
                              encoder_hidden=hiddens,
                              function=decode_function,
                              teacher_forcing_ratio=teacher_forcing_ratio)
        outs = result[0]
        preds = self.parse_outputs_to_tokens(outs)

        return result, preds

    def forward(self, images, answers, categories, alengths=None, questions=None,
                teacher_forcing_ratio=0, decode_function=F.log_softmax):
        """Passes the image and the question through a model and generates answers.
        Args:
            images: Batch of image Variables.
            answers: Batch of answer Variables.
            categories: Batch of answer Variables.
            alengths: List of answer lengths.
            questions: Batch of question Variables.
            teacher_forcing_ratio: Whether to predict with teacher forcing.
            decode_function: What to use when choosing a word from the
                distribution over the vocabulary.
        Returns:
            - outputs: The output scores for all steps in the RNN.
            - hidden: The hidden states of all the RNNs.
            - ret_dict: A dictionary of attributes. See DecoderRNN.py for details.
        """
        # features is (N * HIDDEN_SIZE)
        image_features = self.encode_images(images)


        # Calculate the mus and logvars.

        if(self.bayes==True):
            zs = self.reparameterize(mus, logvars)
        else:
            zs = self.reparameterize_prev(mus, logvars)

        result = self.decode_questions(image_features, zs,
                                       questions=questions,
                                       decode_function=decode_function,
                                       teacher_forcing_ratio=teacher_forcing_ratio)
        return result

    def reconstruct_inputs(self, image_features, category_features):
        """Reconstructs the image features using the VAE.
        Args:
            image_features: Batch of image features.
            category_features: Batch of category features.
        Returns:
            Reconstructed image features and category features.
        """
        recon_image_features = None
        recon_category_features = None
        t_mus, t_logvars, ts = self.encode_into_t(image_features, category_features)

        if self.image_recon:
            recon_image_features = self.image_reconstructor(ts)
        if self.category_space:
            recon_category_features = self.category_reconstructor(ts)

        return recon_image_features, recon_category_features

    def encode_from_category(self, images, categories):
        """Encodes images and categories in t-space.
        Args:
            images: Batch of image Tensors.
            categories: Batch of category Tensors.
        Returns:
            Batch of latent space encodings.
        """
        image_features = self.encode_images(images)
        category_hiddens = self.encode_categories(categories)
        mus, logvars, ts = self.encode_into_t(image_features, category_hiddens)

        return image_features, ts

    def predict_from_category(self, images, categories,
                              questions=None, teacher_forcing_ratio=0,
                              decode_function=F.log_softmax):
        """Outputs the predicted vocab tokens for the categories in a minibatch.
        Args:
            images: Batch of image Variables.
            categories: Batch of category Variables.
            questions: Batch of question Variables.
            teacher_forcing_ratio: Whether to predict with teacher forcing.
            decode_function: What to use when choosing a word from the
                distribution over the vocabulary.
        Returns:
            A tensor with BATCH_SIZE X MAX_LEN where each element is the index
            into the vocab word.
        """
        image_features, zs = self.encode_from_category(images, categories)
        outputs, tokens = self.decode_questions(image_features, zs, questions=questions,
                                              decode_function=decode_function,
                                              teacher_forcing_ratio=teacher_forcing_ratio)

        return tokens