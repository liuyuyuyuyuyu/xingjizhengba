# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import copy
from src.components.episode_buffer import EpisodeBatch
from src.modules.mixers.vdn import VDNMixer
from src.modules.mixers.qmix import QMixer, QAttnMixerV1, QAttnMixerV2
from src.modules.mixers.qtran import QTranBase
import torch as th
from torch.optim import RMSprop
import torch.nn.functional as F


class PONFALLearner:
    def __init__(self, mac, scheme, logger, args):
        self.args = args
        self.mac = mac
        self.logger = logger

        self.params = list(mac.parameters())

        self.last_target_update_episode = 0
        self.self_last_target_update_episode = 0

        self.mixer = None
        if args.mixer is not None:
            if args.mixer == "vdn":
                self.mixer = VDNMixer()
            elif args.mixer == "qmix":
                self.mixer = QMixer(args)
            elif args.mixer == "qmix_attnv1":
                self.mixer = QAttnMixerV1(args, self.mac.input_shape, self.mac.input_alone_shape)
            elif args.mixer == "qmix_attnv2":
                self.mixer = QAttnMixerV2(args, self.mac.input_shape, self.mac.input_alone_shape)
            else:
                raise ValueError("Mixer {} not recognised.".format(args.mixer))
            self.params += list(self.mixer.parameters())
            self.target_mixer = copy.deepcopy(self.mixer)

        self.qtran_mixer = QTranBase(args)  # qtran's mixer

        self.optimiser = RMSprop(params=self.params, lr=args.lr, alpha=args.optim_alpha, eps=args.optim_eps)

        # a little wasteful to deepcopy (e.g. duplicates action selector), but should work for any MAC
        self.target_mac = copy.deepcopy(mac)

        self.log_stats_t = -self.args.learner_log_interval - 1

        # test
        self.device = 'cuda:0' if self.args.use_cuda else 'cpu'

    def train(self, batch: EpisodeBatch, t_env: int, episode_num: int):
        # Get the relevant quantities
        rewards = batch["reward"][:, :-1]
        actions = batch["actions"][:, :-1]
        terminated = batch["terminated"][:, :-1].float()
        mask = batch["filled"][:, :-1].float()
        mask[:, 1:] = mask[:, 1:] * (1 - terminated[:, :-1])
        avail_actions = batch["avail_actions"]

        # Calculate estimated Q-Values
        mac_out = []
        mac_out_interactive = []
        mac_out_interactive_ = []
        mac_out_alone = []
        mac_hidden_states = []  # qtran
        self.mac.init_hidden(batch.batch_size)
        for t in range(batch.max_seq_length):
            agent_outs, agent_outs_interactive, agent_outs_interactive_, agent_outs_alone = self.mac.get_individual_q(
                batch, t=t)
            mac_out.append(agent_outs)
            mac_out_interactive.append(agent_outs_interactive)
            mac_out_interactive_.append(agent_outs_interactive_)
            mac_out_alone.append(agent_outs_alone)
            mac_hidden_states.append(self.mac.hidden_states)  # qtran

        # Calculate the Q-Values necessary for the target
        target_mac_out = []
        self.target_mac.init_hidden(batch.batch_size)
        for t in range(batch.max_seq_length):
            target_agent_outs = self.target_mac.forward(batch, t=t)
            target_mac_out.append(target_agent_outs)

        mac_out = th.stack(mac_out, dim=1)  # Concat over time
        mac_out_interactive = th.stack(mac_out_interactive, dim=1)  # Concat over time
        mac_out_interactive_ = th.stack(mac_out_interactive_, dim=1)  # Concat over time
        mac_out_alone = th.stack(mac_out_alone, dim=1)  # Concat over time
        # Pick the Q-Values for the actions taken by each agent
        chosen_action_qvals = th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(3)  # Remove the last dim

        # --- qtran's nopt loss --- #
        qtran_chosen_action_qvals = chosen_action_qvals
        # ------------------------- #

        # We don't need the first timesteps Q-Value estimate for calculating targets
        target_mac_out = th.stack(target_mac_out[1:], dim=1)  # Concat across time
        # --- qtran opt loss pt.1 --- #
        mac_hidden_states = th.stack(mac_hidden_states, dim=1)
        mac_hidden_states = mac_hidden_states.reshape(batch.batch_size, self.args.n_agents, batch.max_seq_length,
                                                      -1).to(self.device).transpose(1, 2)  # btav
        self.qtran_mixer = self.qtran_mixer.to(self.device)
        joint_qs, vs = self.qtran_mixer(batch[:, :-1], mac_hidden_states[:, :-1])  # use qtran's mixer
        # --------------------------- #

        # Mask out unavailable actions
        target_mac_out[avail_actions[:, 1:] == 0] = -9999999

        # --- qtran opt loss pt.2 --- #
        mac_out_maxs = mac_out.clone()
        mac_out_maxs[avail_actions == 0] = -9999999
        max_actions_qvals, max_actions_current = mac_out_maxs[:, :].max(dim=3, keepdim=True)  # max_actions_qvals: arg1
        # --------------------------- #

        # Max over target Q-Values
        max_actions_current_onehot = None
        if self.args.double_q:
            # Get actions that maximise live Q (for double q-learning)
            mac_out_detach = mac_out.clone().detach()
            mac_out_detach[avail_actions == 0] = -9999999
            cur_max_actions = mac_out_detach[:, 1:].max(dim=3, keepdim=True)[1]
            target_max_qvals = th.gather(target_mac_out, 3, cur_max_actions).squeeze(3)
            # --- qtran opt loss pt.3 --- #
            max_actions_current_ = th.zeros(
                size=(batch.batch_size, batch.max_seq_length, self.args.n_agents, self.args.n_actions),
                device=batch.device)
            max_actions_current_onehot = max_actions_current_.scatter(3, max_actions_current[:, :], 1)
            # --------------------------- #
        else:
            target_max_qvals = target_mac_out.max(dim=3)[0]

        max_joint_qs, _ = self.qtran_mixer(batch[:, :-1], mac_hidden_states[:, :-1],
                                           actions=max_actions_current_onehot[:,
                                                   :-1])

        # Mix
        if self.mixer is not None:
            chosen_action_qvals = self.mixer(chosen_action_qvals, batch["state"][:, :-1])
            target_max_qvals = self.target_mixer(target_max_qvals, batch["state"][:, 1:])

        # Calculate 1-step Q-Learning targets
        targets = rewards + self.args.gamma * (1 - terminated) * target_max_qvals
        # Td-error
        td_error = (chosen_action_qvals - targets.detach())
        mask = mask.expand_as(td_error)
        # 0-out the targets that came from padded data
        masked_td_error = td_error * mask
        # Normal L2 loss, take mean over actual data
        loss = (masked_td_error ** 2).sum() / mask.sum()

        if self.args.regulization == "all":
            # Optimize for 0 interactive
            min_q_interactive = mac_out_interactive_[:, :-1] * avail_actions[:, :-1] * mask.unsqueeze(-1)
            reg_loss = (min_q_interactive ** 2).sum() / mask.unsqueeze(-1).sum()
            loss += reg_loss
        elif self.args.regulization == "chosen_":
            # Optimize for 0 interactive
            chosen_action_qvals_interactive = th.gather(mac_out_interactive_[:, :-1], dim=3, index=actions).squeeze(
                3)  # Remove the last dim
            reg_loss = ((chosen_action_qvals_interactive * mask) ** 2).sum() / mask.sum()
            loss += reg_loss
        elif self.args.regulization == "all_":
            # Optimize for 0 interactive
            min_q_interactive = mac_out_interactive_[:, :-1] * avail_actions[:, :-1] * mask.unsqueeze(-1)
            reg_loss = (min_q_interactive ** 2).sum() / (mask.unsqueeze(-1) * avail_actions[:, :-1]).sum()
            loss += reg_loss
        else:
            reg_loss = th.zeros(1).sum()

        # --- qtran's opt loss --- #
        opt_error = max_actions_qvals[:, :-1].sum(dim=2).reshape(-1, 1) - max_joint_qs.detach() + vs
        masked_opt_error = opt_error * mask.reshape(-1, 1)
        opt_loss = (masked_opt_error ** 2).sum() / mask.sum()
        # ------------------------ #

        # --- qtran's nopt loss --- #
        nopt_values = qtran_chosen_action_qvals.sum(dim=2).reshape(-1,
                                                             1) - joint_qs.detach() + vs  # Don't use target networks here either
        nopt_error = nopt_values.clamp(max=0)
        masked_nopt_error = nopt_error * mask.reshape(-1, 1)
        nopt_loss = (masked_nopt_error ** 2).sum() / mask.sum()
        # ------------------------- #

        qtran_loss = self.args.opt_loss * opt_loss + self.args.nopt_min_loss * nopt_loss
        loss += self.args.alpha * qtran_loss

        # Optimise
        self.optimiser.zero_grad()
        loss.backward()
        grad_norm = th.nn.utils.clip_grad_norm_(self.params, self.args.grad_norm_clip)
        self.optimiser.step()

        if (episode_num - self.self_last_target_update_episode) / (self.args.minus_target_update_interval) >= 1.0:
            self.mac.update_targets()
            self.self_last_target_update_episode = episode_num

        if (episode_num - self.last_target_update_episode) / self.args.target_update_interval >= 1.0:
            self._update_targets()
            self.last_target_update_episode = episode_num

        if t_env - self.log_stats_t >= self.args.learner_log_interval:
            self.logger.log_stat("loss", loss.item(), t_env)
            self.logger.log_stat("reg_loss", reg_loss.item(), t_env)
            self.logger.log_stat("opt_loss", opt_loss.item(), t_env)
            self.logger.log_stat("nopt_loss", nopt_loss.item(), t_env)
            self.logger.log_stat("grad_norm", grad_norm, t_env)
            mask_elems = mask.sum().item()
            self.logger.log_stat("td_error_abs", (masked_td_error.abs().sum().item() / mask_elems), t_env)
            self.logger.log_stat("q_taken_mean",
                                 (chosen_action_qvals * mask).sum().item() / (mask_elems * self.args.n_agents), t_env)
            self.logger.log_stat("target_mean", (targets * mask).sum().item() / (mask_elems * self.args.n_agents),
                                 t_env)
            self.log_stats_t = t_env

    def _update_targets(self):
        self.target_mac.load_state(self.mac)
        if self.mixer is not None:
            self.target_mixer.load_state_dict(self.mixer.state_dict())
        self.logger.console_logger.info("Updated target network")

    def cuda(self):
        self.mac.cuda()
        self.target_mac.cuda()
        if self.mixer is not None:
            self.mixer.cuda()
            self.target_mixer.cuda()

    def save_models(self, path):
        self.mac.save_models(path)
        if self.mixer is not None:
            th.save(self.mixer.state_dict(), "{}/mixer.th".format(path))
        th.save(self.optimiser.state_dict(), "{}/opt.th".format(path))

    def load_models(self, path):
        self.mac.load_models(path)
        # Not quite right but I don't want to save target networks
        self.target_mac.load_models(path)
        if self.mixer is not None:
            self.mixer.load_state_dict(th.load("{}/mixer.th".format(path), map_location=lambda storage, loc: storage))
        self.optimiser.load_state_dict(th.load("{}/opt.th".format(path), map_location=lambda storage, loc: storage))
