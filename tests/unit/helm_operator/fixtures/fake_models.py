from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


def get_fake_model_deploy():
    """FakeMessagesListChatModel that returns a structured deployment plan."""
    return FakeMessagesListChatModel(
        responses=[
            AIMessage(content='{"release_name":"nginx","namespace":"default","chart":"bitnami/nginx"}')
        ]
    )


def get_fake_model_conversational():
    """Model that returns a conversational reply (no tool calls)."""
    return FakeMessagesListChatModel(
        responses=[AIMessage(content="You're welcome! Let me know if you need anything else.")]
    )
