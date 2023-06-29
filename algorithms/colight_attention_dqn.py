import torch
import torch.nn.functional as F
from torch.optim import Adam, SGD, RMSprop
from utils.misc import soft_update, hard_update, enable_gradients, disable_gradients
from utils.agents import AttentionAgent
from utils.critics import AttentionSingleCritic
from utils.misc import onehot_from_logits, categorical_sample
import numpy as np
MSELoss = torch.nn.MSELoss()

data = np.load('datajinan.npy', allow_pickle=True).item()
cloest = data['cloest']
mask = data['mask']

class AttentionDDQN(object):
    """
    Wrapper class for SAC agents with central attention critic in multi-agent
    task
    """
    def __init__(self, agent_init_params, sa_size,
                 gamma=0.95, tau=0.01, pi_lr=0.01, q_lr=0.01,
                 reward_scale=10.,
                 pol_hidden_dim=128,
                 critic_hidden_dim=128, attend_heads=4,
                 **kwargs):
        """
        Inputs:
            agent_init_params (list of dict): List of dicts with parameters to
                                              initialize each agent
                num_in_pol (int): Input dimensions to policy
                num_out_pol (int): Output dimensions to policy
            sa_size (list of (int, int)): Size of state and action space for
                                          each agent
            gamma (float): Discount factor
            tau (float): Target update rate
            pi_lr (float): Learning rate for policy
            q_lr (float): Learning rate for critic
            reward_scale (float): Scaling for reward (has effect of optimal
                                  policy entropy)
            hidden_dim (int): Number of hidden dimensions for networks
        """
        self.nagents = len(sa_size)
        self.s_dim = sa_size[0][0]
        self.a_dim = sa_size[0][1]
        self.central_agent = AttentionAgent(lr=pi_lr,
                                      hidden_dim=pol_hidden_dim,
                                      **agent_init_params[0])
                                    
        # self.agents = [AttentionAgent(lr=pi_lr,
        #                               hidden_dim=pol_hidden_dim,
        #                               **params)
        #                  for params in agent_init_params]
        self.critic = AttentionSingleCritic(sa_size[:5], hidden_dim=critic_hidden_dim,
                                      attend_heads=attend_heads, norm_in = False)
        self.target_critic = AttentionSingleCritic(sa_size[:5], hidden_dim=critic_hidden_dim,
                                             attend_heads=attend_heads, norm_in = False)
        hard_update(self.target_critic, self.critic)
        self.critic_optimizer = Adam(self.critic.parameters(), lr=q_lr,
                                     weight_decay=1e-3)
        self.agent_init_params = agent_init_params
        self.gamma = gamma
        self.tau = tau
        self.pi_lr = pi_lr
        self.q_lr = q_lr
        self.reward_scale = reward_scale
        self.pol_dev = 'cpu'  # device for policies
        self.critic_dev = 'cpu'  # device for critics
        self.trgt_pol_dev = 'cpu'  # device for target policies
        self.trgt_critic_dev = 'cpu'  # device for target critics
        self.niter = 0
        #self.init_from_central_agent()
        # self.grad = AttentionAgent(lr=pi_lr,
        #                               hidden_dim=pol_hidden_dim,
        #                               **agent_init_params[0])


    def step(self, obs, explore=False):
        """
        Take a step forward in environment with all agents
        Inputs:
            observations: List of observations for each agent
        Outputs:
            actions: List of actions for each agent
        """
        # return [a.step(obs, explore=explore) for a, obs in zip(self.agents,
        #                                                        observations)]

        #before = [a.step(obs) for a, obs in zip(self.agents, observations)]

        corrent_5obs = []
        mask_obs = []
        batch_size = obs[0].shape[0]
        tmp_obs = torch.tensor(np.zeros(obs[0].shape), dtype=torch.float32, device=obs[0].device)
        for a_i in range(self.nagents):
            corrent_5obs.append(torch.stack([obs[i] if i != -1 else tmp_obs for i in cloest[a_i]]))
            mask_obs.append([mask[a_i] for _  in range(batch_size)])
        mask_obs = torch.ByteTensor(mask_obs).bool().to(obs[0].device).view(-1,1,len(mask[0]))
        critic_in = list(torch.cat(corrent_5obs,dim = 1))
        
        with torch.no_grad():
            act = self.critic(critic_in, mask=mask_obs, return_all_q=False, return_act=True, explore=explore)

        act = act.view(self.nagents,-1,8)

        return [act[i] for i in range(self.nagents)]

    def update_critic(self, sample, soft=True, logger=None, **kwargs):
        """
        Update central critic for all agents
        """

        obs, acs, rews, next_obs, dones = sample
        # Q loss
        next_acs = []
        next_log_pis = []
        #pi = self.central_agent.target_policy

        agent_num = len(next_obs)
        batch_size = next_obs[0].shape[0]


        next_5obs = []
        #next_5acs = []
        tmp_obs = torch.tensor(np.zeros(next_obs[0].shape), dtype=torch.float32, device=next_obs[0].device)
        #tmp_acs = torch.tensor(np.zeros(next_acs[0].shape), dtype=torch.float32, device=next_obs[0].device)
        next_mask_obs = []
        for a_i in range(self.nagents):
            next_5obs.append(torch.stack([next_obs[i] if i != -1 else tmp_obs for i in cloest[a_i]]))
            #next_5acs.append(torch.stack([next_acs[i] if i != -1 else tmp_acs for i in cloest[a_i]]))

            next_mask_obs.append([mask[a_i] for _  in range(batch_size)])
        
        next_mask_obs = torch.ByteTensor(next_mask_obs).bool().to(next_obs[0].device).view(-1, 1, len(mask[0]))
        
        #5 * (threads*agents) *54
        trgt_critic_in = list(torch.cat(next_5obs,dim = 1))

        corrent_5obs = []
        corrent_5acs = torch.cat(acs)
        mask_obs = []
        for a_i in range(self.nagents):
            corrent_5obs.append(torch.stack([obs[i] if i != -1 else tmp_obs for i in cloest[a_i]]))
            mask_obs.append([mask[a_i] for _  in range(batch_size)])
        
        mask_obs = torch.ByteTensor(mask_obs).bool().to(next_obs[0].device).view(-1,1,len(mask[0]))
        
        critic_in = list(torch.cat(corrent_5obs,dim = 1))

        with torch.no_grad():
            next_q = self.target_critic(trgt_critic_in, mask=next_mask_obs, return_all_q=False, return_q=True)
        
        critic_rets = self.critic(critic_in, regularize=True,return_all_q=True,
                                  logger=logger, niter=self.niter, mask=mask_obs)
        q_loss = 0
        
        all_q, regs = critic_rets

        cur_action = corrent_5acs.max(dim=1, keepdim=True)[1]
        cur_q = all_q.gather(1, cur_action)

        target_q = (torch.cat(rews).view(-1, 1) + self.gamma * next_q *  (1 - torch.cat(dones).view(-1, 1)))

        q_loss += MSELoss(cur_q, target_q.detach())
        
        ''' if regularization
        for reg in regs:
            q_loss += reg  # regularizing attention
        '''

        q_loss.backward()

        # original maac 10 * self.nagents
        # grad_norm = torch.nn.utils.clip_grad_norm(self.critic.parameters(), self.nagents)
        
        
        self.critic_optimizer.step()
        self.critic_optimizer.zero_grad()

        # if logger is not None:
        #     logger.add_scalar('losses/q_loss', q_loss, self.niter)
            #logger.add_scalar('grad_norms/q', grad_norm, self.niter)
        self.niter += 1

    def update_all_targets(self):
        """
        Update all target networks (called after normal updates have been
        performed for each agent)
        """
        soft_update(self.target_critic, self.critic, self.tau)
        # for a in self.agents:
        #     soft_update(a.target_policy, a.policy, self.tau)
        #self.init_from_central_agent()

    def prep_training(self, device='gpu'):
        self.critic.train()
        self.target_critic.train()
        
        if device == 'gpu':
            fn = lambda x: x.cuda()
        else:
            fn = lambda x: x.cpu()
        if not self.critic_dev == device:
            self.critic = fn(self.critic)
            #self.critic_optimizer = fn(self.critic_optimizer)
            self.critic_dev = device
        if not self.trgt_critic_dev == device:
            self.target_critic = fn(self.target_critic)
            self.trgt_critic_dev = device

    def prep_rollouts(self, device='cpu'):
        #self.central_agent.policy.eval()
        self.critic.eval()
        if device == 'gpu':
            fn = lambda x: x.cuda()
        else:
            fn = lambda x: x.cpu()
        if not self.critic_dev == device:
            self.critic = fn(self.critic)
            #self.critic_optimizer = fn(self.critic_optimizer)
            self.critic_dev = device

    def save(self, filename):
        """
        Save trained parameters of all agents into one file
        """
        self.prep_training(device='cpu')  # move parameters to CPU before saving
        save_dict = {'init_dict': self.init_dict,
                     'critic_params': {'critic': self.critic.state_dict(),
                                       'target_critic': self.target_critic.state_dict(),
                                       'critic_optimizer': self.critic_optimizer.state_dict()}}
        torch.save(save_dict, filename)

    @classmethod
    def init_from_env(cls, env, s_dim,a_dim, n_agent ,gamma=0.95, tau=0.01,
                      pi_lr=0.01, q_lr=0.01,
                      reward_scale=10.,
                      pol_hidden_dim=128, critic_hidden_dim=128, attend_heads=4,
                      **kwargs):
        """
        Instantiate instance of this class from multi-agent environment

        env: Multi-agent Gym environment
        gamma: discount factor
        tau: rate of update for target networks
        lr: learning rate for networks
        hidden_dim: number of hidden dimensions for networks
        """
        # agent_init_params = []
        # sa_size = []
        # for acsp, obsp in zip(env.action_space,
        #                       env.observation_space):
        #     agent_init_params.append({'num_in_pol': obsp.shape[0],
        #                               'num_out_pol': acsp.n})
        #     sa_size.append((obsp.shape[0], acsp.n))
        agent_init_params = [{'num_in_pol': s_dim,
                                       'num_out_pol': a_dim} for i in range(n_agent)]
        sa_size = [(s_dim,a_dim) for i in range(n_agent)]

        init_dict = {'gamma': gamma, 'tau': tau,
                     'pi_lr': pi_lr, 'q_lr': q_lr,
                     'reward_scale': reward_scale,
                     'pol_hidden_dim': pol_hidden_dim,
                     'critic_hidden_dim': critic_hidden_dim,
                     'attend_heads': attend_heads,
                     'agent_init_params': agent_init_params,
                     'sa_size': sa_size}
        instance = cls(**init_dict)
        instance.init_dict = init_dict
        return instance

    @classmethod
    def init_from_save(cls, filename, load_critic=False):
        """
        Instantiate instance of this class from file created by 'save' method
        """
        save_dict = torch.load(filename)
        instance = cls(**save_dict['init_dict'])
        instance.init_dict = save_dict['init_dict']
        if load_critic:
            critic_params = save_dict['critic_params']
            instance.critic.load_state_dict(critic_params['critic'])
            instance.target_critic.load_state_dict(critic_params['target_critic'])
            instance.critic_optimizer.load_state_dict(critic_params['critic_optimizer'])
            # for state in instance.critic_optimizer.state.values():
            #     for k, v in state.items():
            #         if torch.is_tensor(v):
            #             state[k] = v.cuda()
        return instance
