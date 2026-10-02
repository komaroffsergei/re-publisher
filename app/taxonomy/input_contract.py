"""Оба классификатора получают один полный вход без скрытого усечения."""
from pathlib import Path


class InputNeedsReview(ValueError):
    pass


class InputGuard:
    def __init__(self, model_dir):
        from tokenizers import Tokenizer
        self.tokenizer = Tokenizer.from_file(str(Path(model_dir) / "input-tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()

    def check(self, text):
        if len(self.tokenizer.encode(text).ids) > 512:
            raise InputNeedsReview("Вход длиннее 512 токенов; нужен ручной разбор")
