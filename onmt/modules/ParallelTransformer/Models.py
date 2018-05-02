import numpy as np
import torch, math
import torch.nn as nn
from onmt.modules.Transformer.Layers import PositionalEncoding
from onmt.modules.Transformer.Layers import EncoderLayer, DecoderLayer
from onmt.modules.ParallelTransformer.Layers import ParallelEncoderLayer
from onmt.modules.BaseModel import NMTModel, Reconstructor
import onmt
from onmt.modules.WordDrop import embedded_dropout
from onmt.modules.Checkpoint import checkpoint

from onmt.modules.Transformer.Layers import XavierLinear, MultiHeadAttention, FeedForward, PrePostProcessing


def custom_layer(module):
    def custom_forward(*args):
        output = module(*args)
        return output
    return custom_forward

class ParallelTransformerEncoder(nn.Module):
    """Encoder in 'Attention is all you need'
    
    Args:
        opt: list of options ( see train.py )
        dicts : dictionary (for source language)
        
    """
    
    def __init__(self, opt, dicts, positional_encoder):
    
        super(ParallelTransformerEncoder, self).__init__()
        
        self.model_size = opt.model_size
        self.n_heads = opt.n_heads
        self.inner_size = opt.inner_size
        self.layers = opt.layers
        self.dropout = opt.dropout
        self.word_dropout = opt.word_dropout
        self.attn_dropout = opt.attn_dropout
        self.emb_dropout = opt.emb_dropout
        self.time = opt.time
        self.version = opt.version
        
        self.word_lut = nn.Embedding(dicts.size(),
                                     self.model_size,
                                     padding_idx=onmt.Constants.PAD)
        
        if opt.time == 'positional_encoding':
            self.time_transformer = positional_encoder
        elif opt.time == 'gru':
            self.time_transformer = nn.GRU(self.model_size, self.model_size, 1, batch_first=True)
        elif opt.time == 'lstm':
            self.time_transformer = nn.LSTM(self.model_size, self.model_size, 1, batch_first=True)
        
        self.preprocess_layer = PrePostProcessing(self.model_size, self.emb_dropout, sequence='d', static=False)
        
        self.postprocess_layer = PrePostProcessing(self.model_size, 0, sequence='n')
        
        self.positional_encoder = positional_encoder
        
        self.layer_modules = nn.ModuleList([ParallelEncoderLayer(self.n_heads, self.model_size, self.dropout, self.inner_size, self.attn_dropout) for _ in range(self.layers)])
    
    def add_layers(self, n_new_layer):
        
        self.new_modules = list()
        self.layers += n_new_layer
        
        for i in range(n_new_layer):
            layer = ParallelEncoderLayer(self.n_heads, self.model_size, self.dropout, self.inner_size, self.attn_dropout) 
            
            # the first layer will use the preprocessing which is the last postprocessing
            if i == 0:
                layer.preprocess_attn.load_state_dict(self.postprocess_layer.state_dict())
                # replace the last postprocessing layer with a new one
                self.postprocess_layer = PrePostProcessing(self.model_size, 0, sequence='n')
            
            self.layer_modules.append(layer)
    
    def mark_pretrained(self):
        
        self.pretrained_point = self.layers
    
    def forward(self, input, grow=False):
        """
        Inputs Shapes: 
            input: batch_size x len_src (wanna tranpose)
        
        Outputs Shapes:
            out: batch_size x len_src x d_model
            mask_src 
            
        """
        
        if grow:
            return self.forward_grow(input)
        
        
        """ Embedding: batch_size x len_src x d_model """
        emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)
        """ Scale the emb by sqrt(d_model) """
        
        if self.time == 'positional_encoding':
            emb = emb * math.sqrt(self.model_size)
        """ Adding positional encoding """
        emb = self.time_transformer(emb)
        if isinstance(emb, tuple):
            emb = emb[0]
        emb = self.preprocess_layer(emb)
        
        mask_src = input.data.eq(onmt.Constants.PAD).unsqueeze(1) # batch_size x len_src x 1 for broadcasting
        
        pad_mask = torch.autograd.Variable(input.data.ne(onmt.Constants.PAD)) # batch_size x len_src
        #~ pad_mask = None
        
        context = emb.contiguous()
        
        memory_bank = list()
        
        for i, layer in enumerate(self.layer_modules):
            
            
            if len(self.layer_modules) - i <= onmt.Constants.checkpointing and self.training:        
                context, norm_input = checkpoint(custom_layer(layer), context, mask_src, pad_mask)
                
                #~ print(type(context))
            else:
                context, norm_input = layer(context, mask_src, pad_mask)      # batch_size x len_src x d_model
            
            if i > 0: # don't keep the norm input of the first layer (a.k.a embedding)
                memory_bank.append(norm_input)
                
        
        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.    
        context = self.postprocess_layer(context)
        
        # make a huge memory bank on the encoder side
        memory_bank.append(context)
        
        memory_bank = torch.stack(memory_bank)
            
        
        return memory_bank, mask_src
        
    def forward_grow(self, input):
        """
        Inputs Shapes: 
            input: batch_size x len_src (wanna tranpose)
        
        Outputs Shapes:
            out: batch_size x len_src x d_model
            mask_src 
            
        """
        
        with torch.no_grad():
            """ Embedding: batch_size x len_src x d_model """
            emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)
            """ Scale the emb by sqrt(d_model) """
            
            if self.time == 'positional_encoding':
                emb = emb * math.sqrt(self.model_size)
            """ Adding positional encoding """
            emb = self.time_transformer(emb)
            if isinstance(emb, tuple):
                emb = emb[0]
            emb = self.preprocess_layer(emb)
            
            mask_src = input.data.eq(onmt.Constants.PAD).unsqueeze(1) # batch_size x len_src x 1 for broadcasting
            
            pad_mask = torch.autograd.Variable(input.data.ne(onmt.Constants.PAD)) # batch_size x len_src
            #~ pad_mask = None
            
            context = emb.contiguous()
            
            memory_bank = list()
            
            for i in range(self.pretrained_point):
                
                layer = self.layer_modules[i]
                
                context, norm_input = layer(context, mask_src, pad_mask)      # batch_size x len_src x d_model
                
                if i > 0: # don't keep the norm input of the first layer (a.k.a embedding)
                    memory_bank.append(norm_input)
                    
        
        for i in range(self.layers - self.pretrained_point):
            layer = self.layer_modules[self.pretrained_point + i]
            
            context, norm_input = layer(context, mask_src, pad_mask)      # batch_size x len_src x d_model
            
            memory_bank.append(norm_input)
        
        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.    
        context = self.postprocess_layer(context)
        
        # make a huge memory bank on the encoder side
        memory_bank.append(context)
        
        memory_bank = torch.stack(memory_bank)
            
        
        return memory_bank, mask_src

class ParallelTransformerDecoder(nn.Module):
    """Encoder in 'Attention is all you need'
    
    Args:
        opt
        dicts 
        
        
    """
    
    def __init__(self, opt, dicts, positional_encoder):
    
        super(ParallelTransformerDecoder, self).__init__()
        
        self.model_size = opt.model_size
        self.n_heads = opt.n_heads
        self.inner_size = opt.inner_size
        self.layers = opt.layers
        self.dropout = opt.dropout
        self.word_dropout = opt.word_dropout 
        self.attn_dropout = opt.attn_dropout
        self.emb_dropout = opt.emb_dropout
        self.time = opt.time
        self.version = opt.version
        
        if opt.time == 'positional_encoding':
            self.time_transformer = positional_encoder
        elif opt.time == 'gru':
            self.time_transformer = nn.GRU(self.model_size, self.model_size, 1, batch_first=True)
        elif opt.time == 'lstm':
            self.time_transformer = nn.LSTM(self.model_size, self.model_size, 1, batch_first=True)
        
        self.preprocess_layer = PrePostProcessing(self.model_size, self.emb_dropout, sequence='d', static=False)
        if self.version == 1.0:
            self.postprocess_layer = PrePostProcessing(self.model_size, 0, sequence='n')
        
        self.word_lut = nn.Embedding(dicts.size(),
                                     self.model_size,
                                     padding_idx=onmt.Constants.PAD)
        
        self.positional_encoder = positional_encoder
        
        self.layer_modules = nn.ModuleList([DecoderLayer(self.n_heads, self.model_size, self.dropout, self.inner_size, self.attn_dropout) for _ in range(self.layers)])
        
        len_max = self.positional_encoder.len_max
        mask = torch.ByteTensor(np.triu(np.ones((len_max,len_max)), k=1).astype('uint8'))
        self.register_buffer('mask', mask)
    
    def renew_buffer(self, new_len):
        
        self.positional_encoder.renew(new_len)
        mask = torch.ByteTensor(np.triu(np.ones((new_len,new_len)), k=1).astype('uint8'))
        self.register_buffer('mask', mask)
    
    def mark_pretrained(self):
        
        self.pretrained_point = self.layers
        
    
    def add_layers(self, n_new_layer):
        
        self.new_modules = list()
        self.layers += n_new_layer
        
        for i in range(n_new_layer):
            layer = DecoderLayer(self.n_heads, self.model_size, self.dropout, self.inner_size, self.attn_dropout) 
            
            # the first layer will use the preprocessing which is the last postprocessing
            if i == 0:
                # layer.preprocess_attn = self.postprocess_layer
                layer.preprocess_attn.load_state_dict(self.postprocess_layer.state_dict())
                # replace the last postprocessing layer with a new one
                self.postprocess_layer = PrePostProcessing(self.model_size, 0, sequence='n')
            
            self.layer_modules.append(layer)
        
    def forward(self, input, context, src, grow=False):
        """
        Inputs Shapes: 
            input: (Variable) batch_size x len_tgt (wanna tranpose)
            context: (Variable) batch_size x len_src x d_model
            mask_src (Tensor) batch_size x len_src
        Outputs Shapes:
            out: batch_size x len_tgt x d_model
            coverage: batch_size x len_tgt x len_src
            
        """
        
        """ Embedding: batch_size x len_tgt x d_model """
        
        if grow:
            return self.forward_grow(input, context, src)

        
        emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)
        if self.time == 'positional_encoding':
            emb = emb * math.sqrt(self.model_size)
        """ Adding positional encoding """
        emb = self.time_transformer(emb)
        if isinstance(emb, tuple):
            emb = emb[0]
        emb = self.preprocess_layer(emb)
        

        mask_src = src.data.eq(onmt.Constants.PAD).unsqueeze(1)
        
        pad_mask_src = torch.autograd.Variable(src.data.ne(onmt.Constants.PAD))
        
        len_tgt = input.size(1)
        mask_tgt = input.data.eq(onmt.Constants.PAD).unsqueeze(1) + self.mask[:len_tgt, :len_tgt]
        mask_tgt = torch.gt(mask_tgt, 0)
        
        output = emb.contiguous()
        
        pad_mask_tgt = torch.autograd.Variable(input.data.ne(onmt.Constants.PAD)) # batch_size x len_src
        pad_mask_src = torch.autograd.Variable(1 - mask_src.squeeze(1))
        
        #~ memory_bank = None
        
        
        for i, layer in enumerate(self.layer_modules):
            
            if len(self.layer_modules) - i <= onmt.Constants.checkpointing and self.training:           
                
                output, coverage = checkpoint(custom_layer(layer), output, context[i], mask_tgt, mask_src, 
                                            pad_mask_tgt, pad_mask_src) # batch_size x len_src x d_model
                
            else:
                output, coverage = layer(output, context[i], mask_tgt, mask_src, 
                                            pad_mask_tgt, pad_mask_src) # batch_size x len_src x d_model
            
            
        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.    
        output = self.postprocess_layer(output)
        
        return output, coverage
        
    def forward_grow(self, input, context, src):
        """
        Inputs Shapes: 
            input: (Variable) batch_size x len_tgt (wanna tranpose)
            context: (Variable) batch_size x len_src x d_model
            mask_src (Tensor) batch_size x len_src
        Outputs Shapes:
            out: batch_size x len_tgt x d_model
            coverage: batch_size x len_tgt x len_src
            
        """
        
        """ Embedding: batch_size x len_tgt x d_model """
        
        with torch.no_grad():
        
            emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)
            if self.time == 'positional_encoding':
                emb = emb * math.sqrt(self.model_size)
            """ Adding positional encoding """
            emb = self.time_transformer(emb)
            if isinstance(emb, tuple):
                emb = emb[0]
            emb = self.preprocess_layer(emb)
            

            mask_src = src.data.eq(onmt.Constants.PAD).unsqueeze(1)
            
            pad_mask_src = torch.autograd.Variable(src.data.ne(onmt.Constants.PAD))
            
            len_tgt = input.size(1)
            mask_tgt = input.data.eq(onmt.Constants.PAD).unsqueeze(1) + self.mask[:len_tgt, :len_tgt]
            mask_tgt = torch.gt(mask_tgt, 0)
            
            output = emb.contiguous()
            
            pad_mask_tgt = torch.autograd.Variable(input.data.ne(onmt.Constants.PAD)) # batch_size x len_src
            pad_mask_src = torch.autograd.Variable(1 - mask_src.squeeze(1))
            
            
            for i in range(self.pretrained_point):
                
                layer = self.layer_modules[i]
                
                output, coverage = layer(output, context[i], mask_tgt, mask_src, 
                                                pad_mask_tgt, pad_mask_src) # batch_size x len_src x d_model
            
        
        for i in range(self.layers - self.pretrained_point):
            layer = self.layer_modules[self.pretrained_point + i]    
            output, coverage = layer(output, context[self.pretrained_point + i], mask_tgt, mask_src, 
                                                pad_mask_tgt, pad_mask_src) # batch_size x len_src x d_model
        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.    
        output = self.postprocess_layer(output)
        
        return output, coverage

    def step(self, input, context, src, buffer=None):
        """
        Inputs Shapes: 
            input: (Variable) batch_size x len_tgt (wanna tranpose)
            context: (Variable) batch_size x len_src x d_model
            mask_src (Tensor) batch_size x len_src
            buffer (List of tensors) List of batch_size * len_tgt-1 * d_model for self-attention recomputing
        Outputs Shapes:
            out: batch_size x len_tgt x d_model
            coverage: batch_size x len_tgt x len_src
            
        """
        
            
        output_buffer = list()
            
        batch_size = input.size(0)
        
        
        input_ = input[:,-1].unsqueeze(1)
        # print(input_.size())
        """ Embedding: batch_size x 1 x d_model """
        emb = self.word_lut(input_)
        
        
        if self.time == 'positional_encoding':
            emb = emb * math.sqrt(self.model_size)
        """ Adding positional encoding """
        if self.time == 'positional_encoding':
            emb = self.time_transformer(emb, t=input.size(1))
        else:
            prev_h = buffer[0] if buffer is None else None
            emb = self.time_transformer(emb, prev_h)
            buffer[0] = emb[1]
            
        if isinstance(emb, tuple):
            emb = emb[0] # emb should be batch_size x 1 x dim
        
            
        # Preprocess layer: adding dropout
        emb = self.preprocess_layer(emb)
        
        # batch_size x 1 x len_src
        mask_src = src.data.eq(onmt.Constants.PAD).unsqueeze(1)
        
        pad_mask_src = torch.autograd.Variable(src.data.ne(onmt.Constants.PAD))
        
        len_tgt = input.size(1)
        mask_tgt = input.data.eq(onmt.Constants.PAD).unsqueeze(1) + self.mask[:len_tgt, :len_tgt]
        # mask_tgt = self.mask[:len_tgt, :len_tgt].unsqueeze(0).repeat(batch_size, 1, 1)
        mask_tgt = torch.gt(mask_tgt, 0)
        mask_tgt = mask_tgt[:, -1, :].unsqueeze(1)
                
        output = emb.contiguous()
        
        pad_mask_tgt = torch.autograd.Variable(input.data.ne(onmt.Constants.PAD)) # batch_size x len_src
        pad_mask_src = torch.autograd.Variable(1 - mask_src.squeeze(1))
        
        memory_bank = None
        
        for i, layer in enumerate(self.layer_modules):
            
            buffer_ = buffer[i] if buffer is not None else None
            assert(output.size(1) == 1)
            output, coverage, buffer_ = layer.step(output, context[i], mask_tgt, mask_src, 
                                        pad_mask_tgt=None, pad_mask_src=None, buffer=buffer_) # batch_size x len_src x d_model
            
            output_buffer.append(buffer_)
            
        
        
        buffer = torch.stack(output_buffer)
        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.    
        output = self.postprocess_layer(output)
            
        
        return output, coverage, buffer



