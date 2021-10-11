#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""
Class for running task-oriented dialogue chats.
"""
from parlai.core.metrics import Metric, LegacyMetric
from parlai.core.message import Message
from parlai.core.opt import Opt
from parlai.core.worlds import World
from parlai.agents.local_human.local_human import LocalHumanAgent
from parlai.utils.misc import display_messages

import parlai.tod.tod_core as tod
import parlai.tod.tod_world_metrics as tod_metrics

import sys
import copy

# Following needs to be kept consistent with opt settings/tod script
USER_UTT_IDX = 0
API_CALL_IDX = 1
API_RESP_IDX = 2
SYSTEM_UTT_IDX = 3
API_DESCRIPTION_PREEMPT_IDX = 4
GOAL_PREEMPT_IDX = 5
AGENT_COUNT = 6

SPEAKER_TO_NAME = {
    USER_UTT_IDX: tod.TodAgentType.USER_UTT_AGENT,
    API_CALL_IDX: tod.TodAgentType.API_CALL_AGENT,
    API_RESP_IDX: tod.TodAgentType.API_RESP_AGENT,
    SYSTEM_UTT_IDX: tod.TodAgentType.SYSTEM_UTT_AGENT,
    API_DESCRIPTION_PREEMPT_IDX: tod.TodAgentType.API_DESCRIPTION_PREEMPT_AGENT,
    GOAL_PREEMPT_IDX: tod.TodAgentType.GOAL_PREEMPT_AGENT,
}

NAME_TO_IDX = {v: k for k, v in SPEAKER_TO_NAME.items()}


class TodWorld(World):
    """
    Base world for running TOD model-model chats. Following agents.

    * User utt agent
    * API call agent
        * Currently assumed to be same as system utt agent in script code, though used as if separate in this world.
    * API responder agent
    * System utt agent
    * API description preempter agent (given to api call + response agent)
    * Goal preempter agent (given to user)

    As is standard for ParlAI, these agents may be models or may be standalone classes that extend the "Agent" class. The models for these *are* expected to have their utterances in a standard format.

    Note that we expect these to be passed in via the opt manually, since some assumptions of regular ParlAI Worlds (ex. task = agent[0], model = agent[1]) are broken here since there is no "task agent" and one agent can be two "roles" (ex. system agent also making API calls)
    """

    def __init__(self, opt: Opt, agents=None, shared=None):
        super().__init__(opt, agents, shared)
        self.batchsize = opt["batchsize"]
        self.batch_agents = []
        self.acts = []
        self.goals = []  # for case when num_episodes < batchsize
        self.tod_world_metrics = []
        for i in range(self.batchsize):
            here_agents = []
            for j, agent in enumerate(agents):
                if (
                    j == SYSTEM_UTT_IDX
                ):  # handle separately cause we expect it to be same as API_CALL agent
                    here_agents.append(here_agents[API_CALL_IDX])
                    continue
                share = agent.share()
                batch_opt = copy.deepcopy(share["opt"])
                batch_opt["batchindex"] = i
                here_agents.append(share["class"](batch_opt, share))
            self.batch_agents.append(here_agents)
            self.acts.append([Message.padding_example()] * 4)
            self.tod_world_metrics.append(tod_metrics.TodMetrics())
        self.end_episode = [False] * self.batchsize

        self.max_turns = self.opt.get("max_turns", 30)
        self.turns = 0
        self.need_preempt = True

    def preempt(self):
        """
        Preempt with goal and schema-based intent descriptions.

        As a logging hack, we stick the schema intent descriptions in as a user
        utterance, but manually pass the value in to the relevant API call/resp agent,
        since passing it to the API call agent elsewhere is a little awkward. Similarly,
        we stick the goal as a system utterance so that it is captured in logging.
        However, we do not pass it in manually, since getting the user utterance will be
        the first turn of `parley()`.
        """
        self._observe_and_act(
            SYSTEM_UTT_IDX,  # Doesn't matter, empty at this point
            USER_UTT_IDX,  # Hack in to a place that'll look nice when printing
            "getting API description preempt. (Must start with `{tod.STANDARD_API_DESCRIPTIONS}`)",
            API_DESCRIPTION_PREEMPT_IDX,
        )

        self._observe_and_act(
            USER_UTT_IDX,
            API_CALL_IDX,
            "responding to api description preempt (empty enter is usually fine) ",
        )
        self._observe_and_act(
            USER_UTT_IDX,
            API_RESP_IDX,
            "responding to api description preempt (empty enter is usually fine)",
        )

        self._observe_and_act(
            SYSTEM_UTT_IDX,  # Doesn't matter for the most part, but want something empty
            SYSTEM_UTT_IDX,  # Hack into a place per comment above
            "getting goal preempt. (Must start with `{tod.STANDARD_GOAL}`)",
            GOAL_PREEMPT_IDX,
        )
        self.goals = [act[SYSTEM_UTT_IDX] for act in self.acts]
        self.turns = 0

    def parley(self):
        if self.need_preempt:
            self.preempt()
            self.need_preempt = False

        else:
            self._observe_and_act(SYSTEM_UTT_IDX, USER_UTT_IDX)
            self._observe_and_act(USER_UTT_IDX, API_CALL_IDX)
            self._observe_and_act(API_CALL_IDX, API_RESP_IDX)
            self._observe_and_act(API_RESP_IDX, SYSTEM_UTT_IDX)

        self.turns += 1
        self.update_counters()

    def _observe_and_act(
        self, observe_idx, act_idx, info="for regular parley", override_act_idx=None
    ):
        act_agent_idx = override_act_idx if override_act_idx else act_idx
        act_agent = self.agents[act_agent_idx]
        record_output_idx = act_idx
        if hasattr(act_agent, "batch_act"):
            batch_observations = []
            for i in range(self.batchsize):
                if not self.end_episode[i]:
                    observe = self.acts[i][observe_idx]
                    observe = self.batch_agents[i][act_agent_idx].observe(observe)
                    batch_observations.append(Message(observe))
                else:
                    # We're done with this episode, so just do a pad.
                    # NOTE: This could cause issues with RL down the line
                    batch_observations.append(Message.padding_example())
                    self.acts[i][record_output_idx] = {"text": "", "id": ""}
            batch_actions = act_agent.batch_act(batch_observations)
            for i in range(self.batchsize):
                if self.end_episode[i]:
                    continue
                self.acts[i][record_output_idx] = batch_actions[i]
                self.batch_agents[i][record_output_idx].self_observe(batch_actions[i])
        else:  # Run on agents individually
            for i in range(self.batchsize):
                act_agent = (
                    self.batch_agents[i][override_act_idx]
                    if override_act_idx
                    else self.batch_agents[i][act_idx]
                )
                if hasattr(act_agent, "episode_done") and act_agent.episode_done():
                    self.end_episode[i] = True
                if self.end_episode[i]:
                    # Following line exists because:
                    # 1. Code for writing converseations is not hapy if an "id" does not exists with a sample
                    # 2. Because of the `self.end_episode` code, no agent will see this example anyway.
                    self.acts[i][record_output_idx] = {"text": "", "id": ""}
                    continue
                act_agent.observe(self.acts[i][observe_idx])
                if isinstance(act_agent, LocalHumanAgent):
                    print(
                        f"Getting message for {SPEAKER_TO_NAME[record_output_idx]} for {info} in batch {i}"
                    )
                try:
                    self.acts[i][record_output_idx] = act_agent.act()
                except StopIteration:
                    self.end_episode[i] = True
        for i in range(self.batchsize):
            if self.end_episode[i]:
                continue
            self.tod_world_metrics[i].handle_message(
                self.acts[i][record_output_idx], SPEAKER_TO_NAME[act_agent_idx]
            )
            if tod.STANDARD_DONE in self.acts[i][record_output_idx].get("text", ""):
                # User models trained to output a "DONE" on last turn; same with human agents.
                self.end_episode[i] = True

    def report(self):
        """
        Report all metrics of all subagents + of this world.
        """

        metrics_separate = []
        for i in range(self.batchsize):
            here_metrics = self.tod_world_metrics[i].report()
            for name, agent in [
                (SPEAKER_TO_NAME[j], self.batch_agents[i][j])
                for j in [USER_UTT_IDX, API_CALL_IDX, API_RESP_IDX, SYSTEM_UTT_IDX]
            ]:
                name_prefix = name[:-6]  # strip "_agent"
                if hasattr(agent, "report"):
                    m = agent.report()
                    if m is None:
                        continue
                    for k, v in m.items():
                        if not isinstance(v, Metric):
                            v = LegacyMetric(v)
                        here_metrics[f"{name_prefix}_{k}"] = v
            metrics_separate.append(here_metrics)
        metrics = metrics_separate[0]
        for i in range(1, self.batchsize):
            for k, v in metrics_separate[i].items():
                if k not in metrics:
                    metrics[k] = v
                else:
                    metrics[k] = metrics[k] + v
        return metrics

    def reset(self):
        super().reset()
        self.need_preempt = True
        self.turns = 0

        self.episode_metrics = []
        self.acts = []
        for i in range(self.batchsize):
            for agent in self.batch_agents[i]:
                agent.reset()
            self.acts.append([None] * 4)
            metrics = self.tod_world_metrics[i].episode_reset()
            if metrics:
                self.episode_metrics.append(metrics)
        self.end_episode = [False] * self.batchsize

    def get_last_episode_metrics(self):
        return self.episode_metrics

    def get_last_episode_goal(self):
        return self.goals

    def episode_done(self):
        if self.turns >= self.max_turns or all(self.end_episode):
            return True
        for i in range(self.batchsize):
            for j in [USER_UTT_IDX, API_CALL_IDX, API_RESP_IDX, SYSTEM_UTT_IDX]:
                if (
                    self.acts[i][j] is not None
                    and tod.STANDARD_DONE in self.acts[i][j].get("text", "")
                ) or (
                    hasattr(self.batch_agents[i][j], "episode_done")
                    and self.batch_agents[i][j].episode_done()
                ):
                    self.end_episode[i] = True
        return all(self.end_episode)

    def epoch_done(self):
        for agent in self.agents:
            if agent.epoch_done():
                return True

    def num_episodes(self):
        result = sys.maxsize
        for agent in self.agents:
            if hasattr(agent, "num_episodes") and agent.num_episodes() > 0:
                result = min(result, agent.num_episodes())
        if result == sys.maxsize:
            return 0
        return result

    def display(self):
        s = "[--batchsize " + str(self.batchsize) + "--]\n"
        for i in range(self.batchsize):
            s += "[batch " + str(i) + ":]\n"
            s += display_messages(
                self.acts[i],
                ignore_agent_reply=self.opt.get("ignore_agent_reply", False),
                add_fields=self.opt.get("display_add_fields", ""),
                prettify=self.opt.get("display_prettify", False),
                max_len=self.opt.get("max_display_len", 1000),
                verbose=self.opt.get("verbose", False),
            )
            s += "\n"
        s += "[--end of batch--]\n"
        return s
