"""Pydantic models: request bodies and explicit response projections (extra fields are dropped)."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DelhiTimestamp = Annotated[
    str,
    Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:30$", description="ISO 8601 in Asia/Kolkata, e.g. 2026-10-23T13:20:01+05:30"),
]
BusinessDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", examples=["2026-10-23"])]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Options(BaseModel):
    A: str
    B: str
    C: str
    D: str




class OptionsIn(Strict):
    A: str = Field(min_length=1, max_length=200)
    B: str = Field(min_length=1, max_length=200)
    C: str = Field(min_length=1, max_length=200)
    D: str = Field(min_length=1, max_length=200)


class QuestionPayloadIn(Strict):
    """A, B, C and D are labels only. answer is the actual text of the correct option."""

    question: str = Field(min_length=1, max_length=500)
    options: OptionsIn
    answer: str = Field(min_length=1, max_length=200, description='Actual correct option text, never a label such as "B"')


class QuestionCreate(Strict):
    question_date: BusinessDate
    question: QuestionPayloadIn


class SubmitBody(Strict):
    response_answer: str = Field(min_length=1, max_length=200, description="Actual text of the selected option (not its label)")


class OpenSubmitBody(Strict):
    email: str = Field(max_length=320, description="Tester's email (open mode only); trimmed and lowercased; not verified")
    response_answer: str = Field(min_length=1, max_length=200, description="Actual text of the selected option (not its label)")


class EmailLogin(Strict):
    email: str = Field(max_length=320, description="Self-declared email; trimmed and lowercased. Ownership is not verified.")




class ErrorField(BaseModel):
    field: str
    message: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    fields: list[ErrorField] | None = None


class ErrorBody(BaseModel):
    error: ErrorDetail


class Meta(BaseModel):
    page: int
    page_size: int
    total: int


class MyResult(BaseModel):
    response_id: int
    question_id: int
    question: str = Field(description="Question text")
    correct_answer: str = Field(description="Actual correct answer text")
    response_answer: str = Field(description="Actual selected answer text")
    is_correct: bool
    responded_at: DelhiTimestamp


class ParticipantPayload(BaseModel):
    """Question and options only: never the answer."""

    question: str
    options: Options


class TodayQuestion(BaseModel):
    question_id: int
    question_date: BusinessDate
    question: ParticipantPayload
    created_at: DelhiTimestamp
    answered: bool
    my_response: MyResult | None


class HistoryItem(MyResult):
    question_date: BusinessDate


class QuestionPayloadOut(BaseModel):
    question: str
    options: Options
    answer: str = Field(description="Actual correct answer text")


class AdminQuestion(BaseModel):
    question_id: int
    question_date: BusinessDate
    question: QuestionPayloadOut
    created_at: DelhiTimestamp
    response_count: int
    is_latest_for_date: bool = Field(description="True for the newest row of its date, which is the one served for that day")


class AuditItem(BaseModel):
    response_id: int
    question_id: int
    question_date: BusinessDate
    user_id: str
    user_name: str
    question: str
    correct_answer: str
    response_answer: str
    is_correct: bool
    responded_at: DelhiTimestamp


class UserItem(BaseModel):
    user_id: str = Field(description="Normalized email (self-declared in email mode)")
    user_name: str = Field(description="Most recent recorded name; equals user_id in email mode")
    response_count: int
    correct_count: int
    first_response_at: DelhiTimestamp | None = Field(description="Null if the user has no responses")
    last_response_at: DelhiTimestamp | None = Field(description="Null if the user has no responses")


class Distribution(BaseModel):
    A: int
    B: int
    C: int
    D: int


class Stats(BaseModel):
    question_id: int
    question_date: BusinessDate
    correct_answer: str = Field(description="Actual correct answer text")
    total: int
    correct: int
    incorrect: int
    distribution: Distribution = Field(description="Responses per option, mapped from stored text back to the option label")
    unmatched: int = Field(description="Responses whose text matches no option (expected 0)")
    accuracy: float | None


class Me(BaseModel):
    user_id: str
    user_name: str
    roles: list[Literal["employee", "admin"]]
    is_admin: bool
    auth_mode: Literal["entra", "email", "open"]
    email_verified: bool = Field(description="false in email mode: the email was typed by the participant, not verified")
    timezone: str
    business_date: BusinessDate
    server_time: DelhiTimestamp


class Gaps(BaseModel):
    from_: BusinessDate = Field(alias="from")
    to: BusinessDate
    missing_dates: list[BusinessDate]

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class DayAnswer(BaseModel):
    user_id: str
    user_name: str
    question_id: int
    question: str
    response_answer: str = Field(description="The option text the user chose")
    correct_answer: str
    is_correct: bool
    responded_at: DelhiTimestamp


class DayUser(BaseModel):
    user_id: str
    user_name: str


class DayAnswers(BaseModel):
    question_date: BusinessDate
    question_ids: list[int] = Field(description="Every question stored for the date (empty if none was scheduled)")
    answered: list[DayAnswer] = Field(description="Every response to a question of that date, ordered by user_id")
    not_answered: list[DayUser] = Field(description="Users registered by that date with no response to any of its questions")


class AuthMode(BaseModel):
    mode: Literal["entra", "email", "open"]
    email_verified: bool = Field(description="false in email mode: participants type their email and it is not verified")
    allowed_domains: list[str]


class Token(BaseModel):
    access_token: str
    token_type: Literal["Bearer"]
    expires_in: int
    user_id: str
    user_name: str = Field(description="The email itself; no name is derived from it")
    authentication_method: Literal["email"]
    email_verified: Literal[False]


class Data[T](BaseModel):
    data: T


class Page[T](BaseModel):
    data: list[T]
    meta: Meta


class Health(BaseModel):
    status: str
    migration: str | None = None


def as_data(value: Any) -> dict[str, Any]:
    return {"data": value}
