"""회의 코드·진행자 복구 키 생성과 비밀값 해시.

- 회의 코드: 헷갈리는 0·O·1·I 를 뺀 32자 중 6자 (K7Q2MX)
- 진행자 복구 키: 같은 문자 집합으로 XXXX-XXXX. 응답에서 한 번만 주고 bcrypt 해시만 저장
- 참가자 비밀번호(숫자 4자리)도 같은 해시 함수를 쓴다
"""

import asyncio
import secrets

import bcrypt

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MEETING_CODE_LENGTH = 6
# bcrypt 비용. 12 면 해시 하나에 수백 ms. 테스트는 conftest 에서 최소값 4 로 낮춘다
BCRYPT_ROUNDS = 12


def _random_chars(n: int) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))


def new_meeting_code() -> str:
    return _random_chars(MEETING_CODE_LENGTH)


def new_recovery_key() -> str:
    return f"{_random_chars(4)}-{_random_chars(4)}"


def _hash(secret: str) -> str:
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def _verify(secret: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(secret.encode(), hashed.encode())
    except ValueError:
        return False


# bcrypt 는 일부러 느린(수백 ms) 함수라 이벤트 루프를 막지 않게 스레드에서 돌린다
async def hash_secret(secret: str) -> str:
    return await asyncio.to_thread(_hash, secret)


async def verify_secret(secret: str, hashed: str) -> bool:
    return await asyncio.to_thread(_verify, secret, hashed)
