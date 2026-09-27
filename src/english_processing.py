from nltk.stem.snowball import EnglishStemmer
from nltk.tokenize import word_tokenize
import enchant

stemmer = EnglishStemmer()
d_us = enchant.Dict('en_US')
d_gb = enchant.Dict('en_GB')

def shortest_available_stem(word: str):
    return stemmer.stem(word)

def _tokenize_message(msg: str):
    try:
        return word_tokenize(msg)
    except:
        return msg.split(' ') #if the real human tokenizer fails, default to naive tokenization instead
    
def is_dictionary_word(word: str) -> bool:
    """In either dictionary. The blacklist and whitelist are NOT consulted here.

    They are keyed by STEM, because that is what a dispute poll records, but this is
    handed a raw word -- so checking them here could never match anything but a word
    that happens to be its own stem. Callers apply rulings after stemming instead.
    """
    return word != '' and (d_us.check(word) or d_gb.check(word))

def is_word_candidate(msg: str) -> bool:
    """A lone word, or a word followed by a parenthesised definition.

    The index guards matter: an attachment-only message tokenises to [], and naive
    splitting of 'word  (def)' yields an empty middle token -- both of which used to
    raise IndexError here and kill the message handler.
    """
    msg_tokens = _tokenize_message(msg)
    if len(msg_tokens) == 1:
        return True
    return len(msg_tokens) > 1 and msg_tokens[1].startswith('(')
    
def get_word_candidate(msg: str) -> str | None:
    msg_tokens = _tokenize_message(msg)
    if len(msg_tokens) > 0:
        first_word = msg_tokens[0].lower()
        if is_word_candidate(msg):
            return first_word