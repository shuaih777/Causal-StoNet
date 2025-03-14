import torch
import torch.nn as nn
import numpy as np


class StoNet_Causal(nn.Module):
    def __init__(self, num_hidden, hidden_dim, input_dim, output_dim, treat_layer, treat_node,
                 CE_treat_weight=None, miss_col=None, obs_ind_node=None, miss_pattern=None):
        """
        initialize the network
        num_hidden: int
            number of hidden layers
        hidden_dim: list of ints
            dimension of each hidden layer
        input_dim: int
            dimension of network input
        output_dim: int
            dimension of network output
        treat_layer: int
            the layer with treatment variable
        treat_node: list of int
            the hidden node that the treatment variables are located at
        CE_treat_weight: tensor
            weight for different labels of treatment variable
        miss_col: tuple
            the index of the columns of covariates that have missing values
        obs_ind_node: list of int
            the hidden node that the observed indicator are located at
        miss_pattern: str
            "mar": missing at random
            "mnar": missing not at random
        """
        super(StoNet_Causal, self).__init__()
        self.num_hidden = num_hidden
        self.treat_layer = treat_layer
        self.treat_node = treat_node
        self.miss_col = miss_col
        self.miss_pattern = miss_pattern
        self.obs_ind_node = obs_ind_node
        self.module_list = []

        self.module_list.append(nn.Linear(input_dim, hidden_dim[0]))
        self.add_module(str(0), self.module_list[0])

        for i in range(self.num_hidden - 1):
            self.module_list.append(nn.Sequential(nn.Tanh(),
                                                  nn.Linear(hidden_dim[i], hidden_dim[i + 1])))
            self.add_module(str(i+1), self.module_list[i+1])

        self.module_list.append(nn.Sequential(nn.Tanh(),
                                              nn.Linear(hidden_dim[-1], output_dim)))
        self.add_module(str(self.num_hidden), self.module_list[self.num_hidden])

        self.prune_flag = 0
        self.mask_prune = None

        # mask to cut the connection from observed indicator to the outcome
        if self.miss_pattern == 'mnar':
            self.mask_mnar = torch.ones_like(self.module_list[self.treat_layer+2][1].weight)
            self.mask_mnar[:, self.obs_ind_node] = 0
            self.mnar_masked_para()

        self.sse = nn.MSELoss(reduction='sum')

        if isinstance(self.treat_node, (list, tuple, np.ndarray)):
            self.treat_loss = nn.CrossEntropyLoss(weight=CE_treat_weight, reduction='sum')
        else:
            self.treat_loss = nn.BCEWithLogitsLoss(pos_weight=CE_treat_weight, reduction='sum')

        if miss_pattern == 'mnar':
            self.obs_ind_loss = nn.BCEWithLogitsLoss(reduction='sum')

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def forward(self, x, treat):
        if self.prune_flag == 1:
            self.prune_masked_para()

        if self.miss_pattern == 'mnar':
            self.mnar_masked_para()

        for layer_index in range(self.num_hidden+1):
            x = self.module_list[layer_index](x)
            if layer_index == self.treat_layer:
                logits = torch.clone(x[:, self.treat_node])
                if isinstance(self.treat_node, (list, tuple, np.ndarray)):
                    ps = torch.softmax(logits, dim=1)
                else:
                    ps = torch.sigmoid(logits)
                x[:, self.treat_node] = treat
        return x, ps

    def set_prune(self, user_mask):
        self.mask_prune = user_mask
        self.prune_flag = 1

    def cancel_prune(self):
        self.prune_flag = 0
        self.mask_prune = None

    def mnar_masked_para(self):
        self.module_list[self.treat_layer+2][1].weight.data *= self.mask_mnar

    def mnar_masked_grad(self):
        self.module_list[self.treat_layer+2][1].weight.grad *= self.mask_mnar

    def prune_masked_para(self):
        for name, para in self.named_parameters():
            para.data[self.mask_prune[name]] = 0

    def prune_masked_grad(self):
        for name, para in self.named_parameters():
            para.grad[self.mask_prune[name]] = 0

    def likelihood_miss(self, x_impute, graph):
        likelihoods = []
        for i in range(len(self.miss_col)):
            # 
            with torch.no_grad():
                # The index set of variables associated with the i-th missing variable, denoted as X2
                graph_idx = graph[i]
                # 提取相关变量的数据子集 (graph_x) 
                graph_x = x_impute[:, graph_idx]
                # 均值向量 μ = [μ1, μ2]
                graph_mean = graph_x.mean(dim=0)
                # 协方差矩阵 Σ = [[Σ_11, Σ_12], [Σ_21, Σ_22]]
                graph_cov = graph_x.T.cov()
                # 计算条件均值的回归系数: Σ_22^(-1) Σ_21
                temp = torch.linalg.solve(graph_cov[1:len(graph_idx), 1:len(graph_idx)], graph_cov[1:len(graph_idx), 0])
                # 计算条件均值: 
                # μ_{1|2} = μ1 + Σ_12 Σ_22^(-1) (X2 - μ2)
                cond_mean = graph_mean[0] + torch.matmul(graph_x[:, 1:len(graph_idx)] -
                                                         graph_mean[1:len(graph_idx)], temp)
                # 计算条件方差:
                # σ²_{1|2} = Σ_11 - Σ_12 Σ_22^(-1) Σ_21
                cond_cov = graph_cov[0, 0] - torch.matmul(temp, graph_cov[1:len(graph_idx), 0])
            # 计算对数似然的一部分:
            # log p(x_1 | X_2) ∝ - (x_1 - μ_{1|2})² / (2 σ²_{1|2})
            likelihoods.append(-self.sse(x_impute[:, self.miss_col[i]], cond_mean)/(2*cond_cov))
        likelihood = sum(likelihoods)
        return likelihood

    def likelihood_latent(self, forward_hidden, hidden_list, layer_index, outcome_loss, sigma_list, y,
                          treat_loss_weight=1, obs_ind_loss_weight=1):
        if layer_index == 0:  # log_likelihood(Y_1|X)
            likelihood = -self.sse(forward_hidden, hidden_list[layer_index]) / (2 * sigma_list[
                layer_index])

        elif layer_index == self.treat_layer:  # log_likelihood(Y_i, A|Y_{i-1})
            z = self.module_list[layer_index](hidden_list[layer_index - 1])

            z_treat = z[:, self.treat_node]
            treat = hidden_list[layer_index][:, self.treat_node]
            likelihood_treat = -self.treat_loss(z_treat, treat) * treat_loss_weight

            if isinstance(self.treat_node, (list, tuple, np.ndarray)):
                lower = self.treat_node[0]
                upper = self.treat_node[-1]
            else:
                lower = self.treat_node
                upper = self.treat_node

            z_rest_1 = z[:, 0:lower]
            temp1 = hidden_list[layer_index][:, 0:lower]
            likelihood_rest_1 = -self.sse(z_rest_1, temp1)/(2 * sigma_list[layer_index])

            z_rest_2 = z[:, upper + 1:]
            temp2 = hidden_list[layer_index][:, upper + 1:]
            likelihood_rest_2 = -self.sse(z_rest_2, temp2)/(2 * sigma_list[layer_index])

            likelihood = likelihood_treat + likelihood_rest_1 + likelihood_rest_2

        elif layer_index == self.num_hidden:  # log_likelihood(Y|Y_h)
            likelihood = -outcome_loss(self.module_list[layer_index](hidden_list[layer_index - 1]), y) / (
                    2 * sigma_list[self.num_hidden])

        else:  # log_likelihood(Y_i|Y_i-1) or log likelihood related to the observed indicator
            if self.miss_pattern == 'mnar':
                if layer_index == self.treat_layer+1:
                    m = self.module_list[layer_index](hidden_list[layer_index - 1])

                    m_obs = m[:, self.obs_ind_node]
                    obs = hidden_list[layer_index][:, self.obs_ind_node]
                    likelihood_obs = -self.obs_ind_loss(m_obs, obs) * obs_ind_loss_weight

                    if isinstance(self.obs_ind_node, (list, tuple, np.ndarray)):
                        lower = self.obs_ind_node[0]
                        upper = self.obs_ind_node[-1]
                    else:
                        lower = self.obs_ind_node
                        upper = self.obs_ind_node

                    m_rest_1 = m[:, 0:lower]
                    temp1 = hidden_list[layer_index][:, 0:lower]
                    likelihood_obs_rest_1 = -self.sse(m_rest_1, temp1)/(2 * sigma_list[layer_index])

                    m_rest_2 = m[:, upper + 1:]
                    temp2 = hidden_list[layer_index][:, upper + 1:]
                    likelihood_obs_rest_2 = -self.sse(m_rest_2, temp2)/(2 * sigma_list[layer_index])

                    likelihood = likelihood_obs + likelihood_obs_rest_1 + likelihood_obs_rest_2
                else:
                    likelihood = -self.sse(self.module_list[layer_index](hidden_list[layer_index - 1]),
                                       hidden_list[layer_index]) / (2 * sigma_list[layer_index])
            else:
                likelihood = -self.sse(self.module_list[layer_index](hidden_list[layer_index - 1]),
                                       hidden_list[layer_index]) / (2 * sigma_list[layer_index])
        return likelihood

    def backward_imputation(self, mh_step, impute_lrs, alpha, outcome_loss, sigma_list, x, treat, y, treat_loss_weight=1,
                            obs_ind_loss_weight=1, graph=None, miss_lr=None, miss_ind=None):
        # initialize momentum term and hidden unit
        hidden_list, momentum_list = [], []
        hidden_list.append(self.module_list[0](x).detach())
        momentum_list.append(torch.zeros_like(hidden_list[-1]))
        for layer_index in range(1, self.num_hidden):
            hidden_list.append(self.module_list[layer_index](hidden_list[-1]).detach())
            momentum_list.append(torch.zeros_like(hidden_list[-1]))
            if layer_index == self.treat_layer:
                hidden_list[-1][:, self.treat_node] = treat
        if self.miss_pattern == 'mnar':
            obs_ind = 1 - miss_ind
            hidden_list[self.treat_layer+1][:, self.obs_ind_node] = obs_ind  # since obs_ind has no connection to later layers

        for i in range(self.num_hidden):
            hidden_list[i].requires_grad = True
        with torch.no_grad():
            forward_hidden = torch.clone(hidden_list[0])

        # initialize momentum term of x imputation
        if self.miss_col is not None:
            x_miss_momentum = torch.zeros_like(x[:, self.miss_col])

        # backward imputation by SGHMC
        for step in range(mh_step):
            # hidden units imputation
            for layer_index in reversed(range(self.num_hidden)):
                hidden_list[layer_index].grad = None

                hidden_likelihood1 = self.likelihood_latent(forward_hidden, hidden_list, layer_index + 1, outcome_loss, sigma_list,
                                                            y, treat_loss_weight, obs_ind_loss_weight)
                hidden_likelihood2 = self.likelihood_latent(forward_hidden, hidden_list, layer_index, outcome_loss, sigma_list,
                                                            y, treat_loss_weight, obs_ind_loss_weight)

                hidden_likelihood1.backward()
                hidden_likelihood2.backward()

                lr = impute_lrs[layer_index]
                with torch.no_grad():
                    momentum_list[layer_index] = (1 - alpha) * momentum_list[layer_index] + lr * hidden_list[
                        layer_index].grad + torch.FloatTensor(hidden_list[layer_index].shape).to(self.device).normal_().mul(
                        np.sqrt(2*alpha))
                    if layer_index == self.treat_layer:
                        # treatment node will not be updated
                        momentum_list[layer_index][:, self.treat_node] = torch.zeros_like(treat)
                    if self.miss_pattern == 'mnar':
                        if layer_index == self.treat_layer+1:
                        # obs_ind node will not be updated
                            momentum_list[layer_index][:, self.obs_ind_node] = torch.zeros_like(obs_ind)

                    hidden_list[layer_index].data += lr * momentum_list[layer_index]
            # missing value imputation
            if self.miss_col is not None:
                x_impute = torch.clone(x.detach())  # x cannot be treated as leaf variable by pytorch, so create x_impute
                x_impute.requires_grad = True
                x_impute.grad = None

                miss_likelihood1 = self.likelihood_miss(x_impute, graph)
                miss_likelihood2 = -self.sse(self.module_list[0](x_impute), hidden_list[layer_index]) / (2 * sigma_list[0])

                miss_likelihood1.backward()
                miss_likelihood2.backward()

                with torch.no_grad():
                    x_miss_momentum = (1 - alpha) * x_miss_momentum + miss_lr * x_impute.grad[:, self.miss_col] + \
                                      torch.FloatTensor(x_impute[:, self.miss_col].shape).to(self.device).normal_().mul(np.sqrt(2*alpha))
                    x_miss_momentum = x_miss_momentum * miss_ind # only update the entries with missing values
                    x[:, self.miss_col] += miss_lr * x_miss_momentum

                    # update the hidden nodes in the first hidden layer after missing value imputation
                    forward_hidden = torch.clone(self.module_list[0](x).detach())

        return hidden_list
