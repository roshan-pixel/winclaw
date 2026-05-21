#!/usr/bin/env python3
from __future__ import annotations
import asyncio, hashlib, re, unicodedata, logging
from dataclasses import dataclass
from typing import Optional, Dict
from urllib.parse import urlparse

logger = logging.getLogger("noise_stripper")

MAX_MESSAGE_CHARS  = 2000
MAX_CHAR_REPEAT    = 2
MAX_PUNCT_REPEAT   = 1
MIN_MEANINGFUL_LEN = 2
ENABLE_CACHE    = True
CACHE_MAX_SIZE  = 512

PIPELINE: Dict[str, bool] = {
    "unicode_normalize":True,"forward_chain":True,"whatsapp_metadata":True,
    "blobs":True,"urls":True,"emojis":True,"collapse_repeats":True,
    "stutter":True,"abbreviations":True,"vtt_artifacts":True,
    "filler_phrases":True,"dedup_sentences":True,"pii_redact":False,
    "whitespace":True,"truncate":True,
}

FILLER_PATTERNS = [
    r"^[-*]{3,}\s*Forwarded\s+[Mm]essage\s*[-*]{3,}\s*$",
    r"^\s*>\s?.*$",r"^<Media omitted>$",
    r"^\[(?:image|video|audio|sticker|document|contact|location)\]$",
    r"^This message was deleted\.?$",r"^Missed (?:voice|video) call\.?$",
    r"^\s*null\s*$",
]

FILLER_PHRASES = frozenset([
    "ok so basically","so basically","basically","just wanted to say",
    "i just wanted to","like i said","you know what i mean","to be honest",
    "tbh","ngl","i mean","well","umm","uhh","hmm","hmmm","uh","um",
    "bhai bhai","yaar yaar","bro bro","matlab","dekh bhai","sun bhai",
    "bol","waise bhi","vaise",
])

ABBREVIATIONS: Dict[str, str] = {
    r"\bbtw\b":"by the way",r"\bimo\b":"in my opinion",
    r"\blmk\b":"let me know",r"\basap\b":"as soon as possible",
    r"\bfyi\b":"for your information",r"\bidk\b":"I don't know",
    r"\bnvm\b":"never mind",r"\brn\b":"right now",
    r"\bwdym\b":"what do you mean",r"\bomg\b":"oh my god",
    r"\bgtg\b":"got to go",r"\bhmu\b":"hit me up",
}

VTT_ARTIFACTS = [
    r"\[inaudible\]",r"\[crosstalk\]",r"\[music\]",r"\[laughter\]",
    r"\bum+\s+um+\b",r"\buh+\s+uh+\b",
]

_INVISIBLE_CHARS = frozenset([
    '\u200b','\u200c','\u200d','\u200e','\u200f','\u202a','\u202b','\u202c',
    '\u202d','\u202e','\u2060','\ufeff','\u00ad',
])

_UNICODE_MAP = str.maketrans({
    '\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"',
    '\u2013':'-','\u2014':'-','\u2026':'...','\u00a0':' ','\u2022':'-',
})

@dataclass
class StripStats:
    original_len:int=0;final_len:int=0;emojis_removed:int=0
    chars_collapsed:int=0;fillers_removed:int=0;urls_shortened:int=0
    sentences_deduped:int=0;pii_redacted:int=0;truncated:bool=False;cache_hit:bool=False
    @property
    def reduction_pct(self):
        return round((1-self.final_len/self.original_len)*100,1) if self.original_len else 0.0
    @property
    def tokens_saved(self): return max(0,(self.original_len-self.final_len)//4)
    def __str__(self):
        return (f"[noise_stripper] {self.original_len}->{self.final_len}c "
                f"({self.reduction_pct}%) saved~{self.tokens_saved}tok "
                f"emojis={self.emojis_removed} fillers={self.fillers_removed} "
                f"urls={self.urls_shortened} deduped={self.sentences_deduped}")

_cache:Dict[str,str]={}
def reset_cache(): _cache.clear()

@dataclass
class UserConfig:
    keep_emojis:bool=False;keep_urls_full:bool=False;expand_abbr:bool=True
    redact_pii:bool=False;redact_otp:bool=False;max_chars:int=MAX_MESSAGE_CHARS

_user_configs:Dict[str,"UserConfig"]={}
def set_user_config(user_id:str,**kwargs): _user_configs[user_id]=UserConfig(**kwargs)
def get_user_config(user_id:str)->UserConfig: return _user_configs.get(user_id,UserConfig())
def get_pipeline_config()->Dict[str,bool]: return dict(PIPELINE)

def _is_emoji(char:str)->bool:
    cp=ord(char)
    return (0x1F600<=cp<=0x1F64F or 0x1F300<=cp<=0x1F5FF or 0x1F680<=cp<=0x1F6FF or
            0x1F700<=cp<=0x1F7FF or 0x1F800<=cp<=0x1F9FF or 0x1FA00<=cp<=0x1FAFF or
            0x2600<=cp<=0x26FF or 0x2700<=cp<=0x27BF or 0xFE00<=cp<=0xFE0F or
            0x1F1E0<=cp<=0x1F1FF or cp in (0xFE0F,0x20E3))

def strip_emojis(text:str,stats:StripStats)->str:
    r=[]
    for c in text:
        if _is_emoji(c): stats.emojis_removed+=1
        else: r.append(c)
    return "".join(r)

def normalize_unicode(text:str)->str:
    text=unicodedata.normalize('NFC',text)
    text=text.translate(_UNICODE_MAP)
    return ''.join(c for c in text if c not in _INVISIBLE_CHARS)

_FWD_HEADER=re.compile(r"[-*]{3,}\s*Forwarded\s+[Mm]essage\s*[-*]{3,}",re.MULTILINE)
_QUOTED_LINE=re.compile(r"^\s*>\s?.*$",re.MULTILINE)
def unwrap_forward_chain(text:str)->str:
    return _QUOTED_LINE.sub("",_FWD_HEADER.sub("",text))

_COMPILED_FILLERS=[re.compile(p,re.IGNORECASE|re.MULTILINE) for p in FILLER_PATTERNS]
def strip_whatsapp_noise(text:str,stats:StripStats)->str:
    for pat in _COMPILED_FILLERS:
        before=len(text); text=pat.sub("",text)
        if len(text)<before: stats.fillers_removed+=1
    return text

_REPEAT_CHARS=re.compile(r'(.)\1{2,}')
_REPEAT_PUNCT=re.compile(r'([!?.,;:]){2,}')
_REPEAT_SPACES=re.compile(r'[ \t]{2,}')
_REPEAT_NEWLINES=re.compile(r'\n{3,}')
def collapse_repetitions(text:str,stats:StripStats)->str:
    before=len(text)
    text=_REPEAT_CHARS.sub(r'\1\1',text)
    text=_REPEAT_PUNCT.sub(r'\1',text)
    text=_REPEAT_SPACES.sub(' ',text)
    text=_REPEAT_NEWLINES.sub('\n\n',text)
    stats.chars_collapsed+=before-len(text)
    return text

_STUTTER=re.compile(r'\b(\w+)(\s+\1){1,}\b',re.IGNORECASE)
def remove_stutter(text:str)->str: return _STUTTER.sub(r'\1',text)

_COMPILED_ABBR=[(re.compile(p,re.IGNORECASE),e) for p,e in ABBREVIATIONS.items()]
def expand_abbreviations(text:str)->str:
    for pat,exp in _COMPILED_ABBR: text=pat.sub(exp,text)
    return text

_VTT_COMPILED=[re.compile(p,re.IGNORECASE) for p in VTT_ARTIFACTS]
def clean_vtt_artifacts(text:str)->str:
    for pat in _VTT_COMPILED: text=pat.sub('',text)
    return text

def strip_filler_phrases(text:str,stats:StripStats)->str:
    text=text.strip(); lower=text.lower()
    for phrase in sorted(FILLER_PHRASES,key=len,reverse=True):
        if lower.startswith(phrase):
            text=text[len(phrase):].lstrip(' ,.\n')
            stats.fillers_removed+=1; lower=text.lower()
    return text

_URL_RE=re.compile(r'https?://[^\s]{20,}',re.IGNORECASE)
def shorten_urls(text:str,stats:StripStats)->str:
    def _r(m):
        try: stats.urls_shortened+=1; return f"[{urlparse(m.group(0)).netloc}]"
        except: return m.group(0)
    return _URL_RE.sub(_r,text)

_SENTENCE_SPLIT=re.compile(r'(?<=[.!?])\s+')
def _bigram_sim(a:str,b:str)->float:
    if not a or not b: return 0.0
    def bg(s): return set(s[i:i+2] for i in range(len(s)-1))
    ba,bb=bg(a),bg(b)
    return len(ba&bb)/len(ba|bb) if ba and bb else 0.0

def deduplicate_sentences(text:str,stats:StripStats,fuzzy:bool=True,threshold:float=0.80)->str:
    sentences=_SENTENCE_SPLIT.split(text); seen=[]; result=[]
    for s in sentences:
        key=re.sub(r'\s+',' ',s.strip().lower())
        if not key: continue
        is_dup=any(_bigram_sim(key,p)>=(threshold if fuzzy else 1.0) for p in seen)
        if not is_dup: seen.append(key); result.append(s)
        else: stats.sentences_deduped+=1
    return ' '.join(result)

_BASE64_RE=re.compile(r'[A-Za-z0-9+/]{60,}={0,2}')
def strip_blobs(text:str)->str: return _BASE64_RE.sub('[data]',text)

_EMAIL_RE=re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}")
_AADHAAR_RE=re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
def redact_pii(text:str,stats:StripStats,redact_otp:bool=False)->str:
    text,n=_AADHAAR_RE.subn('[AADHAAR]',text); stats.pii_redacted+=n
    text,n=_EMAIL_RE.subn('[EMAIL]',text); stats.pii_redacted+=n
    return text

def smart_truncate(text:str,max_chars:int,stats:StripStats)->str:
    if len(text)<=max_chars: return text
    head_len=int(max_chars*0.80); tail_len=max_chars-head_len-7
    head=text[:head_len].rsplit(' ',1)[0]
    tail=text[-tail_len:].split(' ',1)[-1] if tail_len>0 else ""
    stats.truncated=True
    return f"{head} [...] {tail}"

def strip_and_normalize(text:str,user_id:str="unknown",keep_emojis:bool=False,
                        log_stats:bool=True,pipeline:Optional[Dict[str,bool]]=None)->str:
    if not text or not text.strip(): return text
    cfg=get_user_config(user_id); p={**PIPELINE,**(pipeline or {})}
    if ENABLE_CACHE:
        ck=hashlib.md5(text.encode()).hexdigest()
        if ck in _cache: return _cache[ck]
    stats=StripStats(original_len=len(text))
    if p.get("unicode_normalize"): text=normalize_unicode(text)
    if p.get("forward_chain"): text=unwrap_forward_chain(text)
    if p.get("whatsapp_metadata"): text=strip_whatsapp_noise(text,stats)
    if p.get("blobs"): text=strip_blobs(text)
    if p.get("urls") and not cfg.keep_urls_full: text=shorten_urls(text,stats)
    if p.get("emojis") and not keep_emojis and not cfg.keep_emojis: text=strip_emojis(text,stats)
    if p.get("collapse_repeats"): text=collapse_repetitions(text,stats)
    if p.get("stutter"): text=remove_stutter(text)
    if p.get("abbreviations") and cfg.expand_abbr: text=expand_abbreviations(text)
    if p.get("vtt_artifacts"): text=clean_vtt_artifacts(text)
    if p.get("filler_phrases"): text=strip_filler_phrases(text,stats)
    if p.get("dedup_sentences"): text=deduplicate_sentences(text,stats,fuzzy=True)
    if p.get("pii_redact") or cfg.redact_pii: text=redact_pii(text,stats,redact_otp=cfg.redact_otp)
    if p.get("whitespace"):
        text=text.strip()
        text=re.sub(r'[ \t]+',' ',text)
        text=re.sub(r'\n{3,}','\n\n',text)
    if p.get("truncate"): text=smart_truncate(text,cfg.max_chars,stats)
    stats.final_len=len(text)
    if log_stats and stats.reduction_pct>5: logger.info(f"[{user_id}] {stats}")
    if len(text.strip())<MIN_MEANINGFUL_LEN: return "[empty message]"
    if ENABLE_CACHE:
        if len(_cache)>=CACHE_MAX_SIZE: _cache.pop(next(iter(_cache)))
        _cache[ck]=text
    return text

async def async_strip_and_normalize(text:str,user_id:str="unknown",**kwargs)->str:
    loop=asyncio.get_event_loop()
    return await loop.run_in_executor(None,lambda: strip_and_normalize(text,user_id=user_id,**kwargs))

def normalize_history(messages:list,max_messages:int=20,max_system_len:int=2000,user_id:str="unknown")->list:
    system_msgs=[m for m in messages if m.get("role")=="system"]
    non_system=[m for m in messages if m.get("role")!="system"][-max_messages:]
    cleaned=[{**m,"content":strip_and_normalize(m.get("content",""),user_id=user_id,log_stats=False)
              if isinstance(m.get("content",""),str) else m.get("content","")} for m in non_system]
    trimmed_system=[{**sm,"content":sm.get("content","")[:max_system_len]+"\n[truncated]"
                    if isinstance(sm.get("content",""),str) and len(sm.get("content",""))>max_system_len
                    else sm.get("content","")} for sm in system_msgs]
    return trimmed_system+cleaned

def estimate_tokens(text:str)->int: return max(1,len(text)//4)