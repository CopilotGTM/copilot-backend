from typing import Literal

from pydantic import BaseModel

MeetingRecordingBotJoinPolicy = Literal["all", "external_only", "never"]


class MeetingRecordingPolicyResponse(BaseModel):
    bot_join_policy: MeetingRecordingBotJoinPolicy
    bot_join_policy_options: list[dict[str, str]]


class UpdateMeetingRecordingPolicyRequest(BaseModel):
    bot_join_policy: MeetingRecordingBotJoinPolicy
