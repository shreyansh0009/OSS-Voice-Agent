from abc import ABC, abstractmethod


class BaseTTS(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """
        Convert text to speech.

        Returns:
            Raw PCM audio bytes (16-bit, mono, 16kHz or 8kHz depending on provider)
        """
        ...
