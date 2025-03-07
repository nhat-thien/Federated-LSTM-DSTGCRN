"""
LSTM-DSTGCRN model, serves as local model for the federated learning framework.
Codes are adapted from the DSTGCRN model 'https://github.com/PalaceTony/DSTGCRN'
---
Thien Pham, Oct 2024
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import MultiheadAttention
from collections import OrderedDict
dynamic_embed = None


class AVWGCN(nn.Module):
    def __init__(self, dim_in, dim_out, cheb_k, embed_dim, hyperGNN_dim1, hyperGNN_dim2):
        super(AVWGCN, self).__init__()
        self.cheb_k = cheb_k
        self.weights_pool = nn.Parameter(
            torch.FloatTensor(embed_dim, cheb_k, dim_in, dim_out)
        )
        self.bias_pool = nn.Parameter(torch.FloatTensor(embed_dim, dim_out))
        self.hyperGNN_dim1 = hyperGNN_dim1
        self.hyperGNN_dim2 = hyperGNN_dim2
        self.embed_dim = embed_dim
        self.fc = nn.Sequential(
            OrderedDict(
                [
                    ("fc1", nn.Linear(dim_in, self.hyperGNN_dim1)),
                    ("sigmoid1", nn.Sigmoid()),
                    ("fc2", nn.Linear(self.hyperGNN_dim1, self.hyperGNN_dim2)),
                    ("sigmoid2", nn.Sigmoid()),
                    ("fc3", nn.Linear(self.hyperGNN_dim2, self.embed_dim)),
                ]
            )
        )

    def forward(self, x, node_embeddings):
        if dynamic_embed:
            # x shaped [B, N, C], node_embeddings shaped [B, N, D] -> supports shaped [B, N, N]
            # output shape [B, N, C]
            B, N = node_embeddings.shape[0], node_embeddings.shape[1]
            
            # Cache the identity matrix if not already cached or if N changes
            if not hasattr(self, 'cached_identity'):
                self.cached_identity = torch.eye(N, device=node_embeddings.device, dtype=node_embeddings.dtype).unsqueeze(0)
            
            supports1 = self.cached_identity.repeat(B, 1, 1)  # B, N, N

            filter = self.fc(x)  # B, N, D
            nodevec = torch.tanh(node_embeddings * filter)  # B, N, D

            # Calculate Laplacian supports
            supports = AVWGCN.get_laplacian(
                F.relu(torch.matmul(nodevec, nodevec.transpose(2, 1))), supports1
            )

            # Build Chebyshev polynomials (supports) up to self.cheb_k
            support_set = [supports1, supports]
            for k in range(2, self.cheb_k):
                support_set.append(2 * torch.matmul(supports, support_set[-1]) - support_set[-2])
            supports = torch.stack(support_set, dim=1)  # B, cheb_k, N, N

            # Proceed with the rest of the forward computation
            x_g = torch.einsum("bknm,bmc->bknc", supports, x)  # B, cheb_k, N, C

            weights = torch.einsum("bnd,dkio->bnkio", node_embeddings, self.weights_pool)  # B, N, cheb_k, C_in, C_out
            bias = torch.matmul(node_embeddings, self.bias_pool)  # B, N, C_out

            x_g = x_g.permute(0, 2, 1, 3)  # B, N, cheb_k, C_in
            x_gconv = torch.einsum("bnki,bnkio->bno", x_g, weights) + bias  # B, N, C_out

        else:
            batch_size = x.shape[0]
            node_embeddings = node_embeddings.squeeze()
            node_num = node_embeddings.shape[0]
            supports1 = torch.eye(node_num, device=node_embeddings.device, dtype=node_embeddings.dtype)
            filter = self.fc(x)
            node_embeddings_expanded = node_embeddings.unsqueeze(0).expand(batch_size, -1, -1)  # Shape: [B, N, D]
            nodevec = torch.tanh(
                torch.mul(node_embeddings_expanded, filter)
            )  # [B,N,dim_in]

            supports2 = AVWGCN.get_laplacian(
                F.relu(torch.matmul(nodevec, nodevec.transpose(2, 1))), supports1
            )
            supports3 = AVWGCN.get_laplacian(
                F.relu(torch.matmul(nodevec, nodevec.transpose(2, 1))), supports2
            )

            x_g1 = torch.einsum("nm,bmc->bnc", supports1, x)
            x_g2 = torch.einsum("bnm,bmc->bnc", supports2, x)
            x_g3 = torch.einsum("bnm,bmc->bnc", supports3, x)

            x_g = torch.stack([x_g1, x_g2, x_g3], dim=1)

            weights = torch.einsum(
                "nd,dkio->nkio", node_embeddings, self.weights_pool
            )  # B, N, cheb_k, dim_in, dim_out
            bias = torch.matmul(node_embeddings, self.bias_pool)  # B, N, dim_out

            # x_g = torch.einsum("bknm,bmc->bknc", supports, x)  # B, cheb_k, N, dim_in
            x_g = x_g.permute(0, 2, 1, 3)  # B, N, cheb_k, dim_in
            x_gconv = (
                torch.einsum("bnki,nkio->bno", x_g, weights) + bias
            )  # B, N, dim_out
            supports = x_g

        return x_gconv, supports[:, 1, :, :]

    @staticmethod
    def get_laplacian(graph, I, normalize=True):
        if normalize:
            graph = graph + I
            D = torch.diag_embed(torch.sum(graph, dim=-1) ** (-1 / 2))
            L = torch.matmul(torch.matmul(D, graph), D)

        return L


import torch
import torch.nn as nn

class AGCRNCell(nn.Module):
    def __init__(self, node_num, dim_in, dim_out, cheb_k, embed_dim, hyperGNN_dim1, hyperGNN_dim2):
        super(AGCRNCell, self).__init__()
        self.node_num = node_num
        self.hidden_dim = dim_out

        # Define gate and update modules
        self.gate = AVWGCN(dim_in + self.hidden_dim, 2 * dim_out, cheb_k, embed_dim, hyperGNN_dim1, hyperGNN_dim2)
        self.update = AVWGCN(dim_in + self.hidden_dim, dim_out, cheb_k, embed_dim, hyperGNN_dim1, hyperGNN_dim2)

    def forward(self, x, state, node_embeddings):
        # x: B, num_nodes, input_dim
        # state: B, num_nodes, hidden_dim
        state = state.to(x.device)
        input_and_state = torch.cat((x, state), dim=-1)
        
        # Compute gate values
        z_r, adjmatrix = self.gate(input_and_state, node_embeddings)
        z_r = torch.sigmoid(z_r)
        z, r = torch.split(z_r, self.hidden_dim, dim=-1)
        
        # Compute candidate hidden state
        candidate = torch.cat((x, z * state), dim=-1)
        hc, _ = self.update(candidate, node_embeddings)
        hc = torch.tanh(hc)
        
        # Compute new hidden state using update gate
        h = r * state + (1 - r) * hc  # B, num_nodes, hidden_dim
        return h, adjmatrix

    def init_hidden_state(self, batch_size):
        return torch.zeros(batch_size, self.node_num, self.hidden_dim, dtype=torch.float32)


class AVWDCRNN(nn.Module):
    def __init__(self, node_num, dim_in, dim_out, cheb_k, embed_dim, hyperGNN_dim1, hyperGNN_dim2, num_layers=1):
        super(AVWDCRNN, self).__init__()
        assert num_layers >= 1, "At least one DCRNN layer in the Encoder."
        self.node_num = node_num
        self.input_dim = dim_in
        self.num_layers = num_layers
        self.dcrnn_cells = nn.ModuleList()
        self.dcrnn_cells.append(AGCRNCell(node_num, dim_in, dim_out, cheb_k, embed_dim, hyperGNN_dim1, hyperGNN_dim2))
        for _ in range(1, num_layers):
            self.dcrnn_cells.append(
                AGCRNCell(node_num, dim_out, dim_out, cheb_k, embed_dim, hyperGNN_dim1, hyperGNN_dim2)
            )
        # Initialize adjacency matrix, considering the shape and data type you need
        # self.adjmatrix = torch.zeros(32, node_num, node_num)

    def forward(self, x, init_state, node_embeddings):
        # shape of x: (B, T, N, D)
        # shape of init_state: (num_layers, B, N, hidden_dim)
        # shape of node_embeddings: (T, N, D)
        assert x.shape[2] == self.node_num and x.shape[3] == self.input_dim
        seq_length = x.shape[1]
        current_inputs = x
        output_hidden = []
        for i in range(self.num_layers):
            state = init_state[i]
            inner_states = []
            adjmatrices = []
            for t in range(seq_length):
                if dynamic_embed:
                    state, adjmatrix = self.dcrnn_cells[i](
                        current_inputs[:, t, :, :], state, node_embeddings[:, t, :, :]
                    )
                    adjmatrices.append(adjmatrix)

                else:
                    state, adjmatrix = self.dcrnn_cells[i](
                        current_inputs[:, t, :, :], state, node_embeddings
                    )
                    adjmatrices.append(adjmatrix)
                inner_states.append(state)
            output_hidden.append(state)
            current_inputs = torch.stack(inner_states, dim=1)
        # current_inputs: the outputs of last layer: (B, T, N, hidden_dim)
        # output_hidden: the last state for each layer: (num_layers, B, N, hidden_dim)
        # last_state: (B, N, hidden_dim)
        # adjmatrices: adj for the last layer
        return current_inputs, output_hidden, adjmatrices

    def init_hidden(self, batch_size):
        init_states = []
        for i in range(self.num_layers):
            init_states.append(self.dcrnn_cells[i].init_hidden_state(batch_size))
        return torch.stack(init_states, dim=0)  # (num_layers, B, N, hidden_dim)


class DynamicEmbedding(nn.Module):
    def __init__(self, args, input_dim, embed_dim, hidden_dim_node):
        super(DynamicEmbedding, self).__init__()
        self.num_nodes = args.num_nodes
        self.embed_dim = embed_dim
        self.num_layers = args.num_layers_node
        self.gru_layer = args.gru_layer
        self.lstm_layer = args.lstm_layer
        
        if self.gru_layer:
            self.sequential_model = nn.GRU(
                input_dim, hidden_dim_node, num_layers=self.num_layers, batch_first=True
            )
            self.fc = nn.Linear(hidden_dim_node, embed_dim)
        elif self.lstm_layer:
            self.sequential_model = nn.LSTM(
                input_dim, hidden_dim_node, num_layers=self.num_layers, batch_first=True
            )
            self.fc = nn.Linear(hidden_dim_node, embed_dim)
        else: 
            self.fc1 = nn.Linear(input_dim, hidden_dim_node)
            self.fc2 = nn.Linear(hidden_dim_node, embed_dim)

        self.attention_layer = args.attention_layer
        if self.attention_layer:
            self.self_attn = MultiheadAttention(embed_dim, num_heads=args.num_heads)

    def forward(self, x):
        # x is of shape (batch_size, timesteps, N, input_dim)
        x = x.permute(0, 2, 1, 3)  # (batch_size, N, timesteps, input_dim)
        x = x.reshape(
            -1, x.shape[2], x.shape[3]
        )  # (batch_size*N, timesteps, input_dim)
        
        if self.gru_layer or self.lstm_layer:
            out, _ = self.sequential_model(
                x
            )  # out is of shape (batch_size*N, timesteps, hidden_dim)
            out = self.fc(out)  # (batch_size*N, timesteps, embed_dim)
        else:
            out = self.fc1(x)
            out = self.fc2(out)
        
        
        if not self.attention_layer:
            out = F.relu(out)  # Apply ReLU activation
            out = out.reshape(
                x.shape[0] // self.num_nodes, self.num_nodes, -1, self.embed_dim
            )  # (batch_size, N, timesteps, embed_dim)
            attn_output = out
            attn_output = attn_output.permute(
                0, 2, 1, 3
            )  # (batch_size, timesteps, N, embed_dim)
        else:
            out = F.relu(out)  # Apply ReLU activation
            out = out.permute(1, 0, 2)  # (timesteps, batch_size*N, embed_dim)
            attn_output, _ = self.self_attn(out, out, out)  # Apply self-attention
            attn_output = attn_output.permute(
                1, 0, 2
            )  # (batch_size*N, timesteps, embed_dim)

            attn_output = attn_output.reshape(
                x.shape[0] // self.num_nodes, self.num_nodes, -1, self.embed_dim
            )  # (batch_size, N, timesteps, embed_dim)
            attn_output = attn_output.permute(
                0, 2, 1, 3
            )  # (batch_size, timesteps, N, embed_dim)
        
        return attn_output


class LSTM_DSTGCRN(nn.Module):
    def __init__(self, args):
        super(LSTM_DSTGCRN, self).__init__()

        # Here, I set the number of nodes to 1 for dummy calls, i.e.,
        # for the calls where num_nodes does not matter.
        self.model_name = "LSTM-DSTGCRN"
        args.num_nodes = 1 if not hasattr(args, 'num_nodes') else args.num_nodes
        self.num_nodes = args.num_nodes
        self.input_dim = args.input_dim
        self.hidden_dim = args.rnn_units
        self.hidden_dim_node = args.hidden_dim_node
        self.output_dim = args.output_dim
        self.lookahead = args.lookahead
        self.num_layers = args.num_layers
        self.embed_dim = args.embed_dim
        self.TNE = args.TNE
        self.batch = args.batch_size

        # For FL training
        self.layer_scores = []

        # Ablation study
        global dynamic_embed
        dynamic_embed = args.dynamic_embed

        # Instantiate the DynamicEmbedding
        if self.TNE:
            self.node_embeddings = nn.Parameter(
                torch.randn(self.batch, args.lookback, self.num_nodes, args.embed_dim, dtype=torch.float32),
                requires_grad=True,
            )
        else:
            if dynamic_embed:
                self.dynamic_embedding = DynamicEmbedding(
                    args, self.input_dim, self.embed_dim, self.hidden_dim_node
                )
            else:
                # Ablation study for static node_embeddings
                self.node_embeddings = nn.Parameter(
                    torch.randn(1, self.num_nodes, args.embed_dim, dtype=torch.float32), requires_grad=True
                )

        self.encoder = AVWDCRNN(
            args.num_nodes,
            args.input_dim,
            args.rnn_units,
            args.cheb_k,
            args.embed_dim,
            args.hyperGNN_dim1, 
            args.hyperGNN_dim2,
            args.num_layers,
        )

        # Predictor
        self.end_conv = nn.Conv2d(
            1,
            args.lookahead * self.output_dim,
            kernel_size=(1, self.hidden_dim),
            bias=True,
        )

    def forward(self, source):
        # Source: B, T_1, N, D
        # Target: B, T_2, N, D

        # Compute the dynamic node embeddings
        if self.TNE:
            node_embeddings = self.node_embeddings

        else:
            if dynamic_embed:
                node_embeddings = self.dynamic_embedding(source)  # B, T_1, N, embed_dim
            else:
                node_embeddings = self.node_embeddings

        init_state = self.encoder.init_hidden(
            source.shape[0]
        )  # num_layers, B, N, hidden
        output, _, adjmatrices = self.encoder(
            source, init_state, node_embeddings
        )  # B, T, N, hidden
        output = output[:, -1:, :, :]  # B, 1, N, hidden

        # CNN based predictor
        output = self.end_conv((output))  # B, T*C, N, 1
        output = output.squeeze(-1).reshape(
            -1, self.lookahead, self.output_dim, self.num_nodes
        )
        output = output.permute(0, 1, 3, 2)  # B, T, N, C
        adjmatrices = torch.stack(adjmatrices, dim=1)

        return output, adjmatrices
    
    def get_weights(self):
        names = []
        weights = []
        for name, param in self.named_parameters():
            if param.requires_grad:  # Only include trainable weights
                weights.append(param.data.cpu().numpy())  # Move to CPU and convert to NumPy array
                names.append(name)
        return [weights, names]
    
    def set_weights(self, weights):
        for param, weight in zip(self.parameters(), weights):
            param.data = torch.from_numpy(weight).to(param.device).type(param.dtype)
