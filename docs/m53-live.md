# M5.3: Meta Muse live readiness

## First Spark failure: diagnostic procedure (2026-11-04)

The first live report `artifacts/m53-live/d248e486874d40e1b065452c6c2652ca/report.json`
records two HTTP 200 planning failures, each using exactly the 1200 output-token
cap. It retains no response status, incomplete reason, reasoning-token breakdown
or output item metadata. Output-budget exhaustion is the leading hypothesis,
**not a proven diagnosis**; malformed JSON or an unsupported response shape cannot
be excluded retrospectively. Do not recreate a supposed original server response.

Official references checked 2026-11-04:

- [Responses](https://dev.meta.ai/docs/protocols/responses): `text.format` configures
  structured output. `output_text` is an SDK helper; raw HTTP uses output items.
- [Reasoning](https://dev.meta.ai/docs/reasoning): reasoning counts within
  `max_output_tokens`; Responses uses `reasoning: {effort: "low"}` (or `minimal`).
  Omitting effort leaves model-controlled reasoning; `none` is unsupported.
- [Schemas](https://dev.meta.ai/docs/api-reference/responses/schemas): inspect
  response status, `incomplete_details`, `usage.output_tokens_details.reasoning_tokens`
  and message/reasoning item types and statuses.
- [Strict output](https://dev.meta.ai/docs/structured-output): retain
  `text.format.strict=true`, required properties and `additionalProperties=false`.

The provider now records bounded, allowlisted `response_metadata` on its incurred
call record, before parsing/validation: status/reason labels, reasoning-token count
(a subset of billed output, never added twice), output-item types/status/role/phase,
content types and output-text character counts. Unknown strings become `other`.
No raw prompts, generated text, private reasoning, summaries, encrypted reasoning,
error messages or unredacted bodies are retained. `output_token_cap_reached` is
only an observed count comparison; it does not by itself prove truncation.

`TRUSTFLOW_LLM_REASONING_EFFORT=low|minimal` is optional; unset preserves existing
application behavior. The matrix's output cap and default request format stay
unchanged. Do not silently retry an incomplete or invalid HTTP 200 response.

Use a separate single-request diagnostic before any further live matrix. It
calls the same provider/planning schema once, validates the returned steps with
the application's actual typed allowlist/preconditions and deterministic policy,
then checks the fixed read-only goal. It performs no repair, semantic inference,
approval or tool execution. Each invocation creates a fresh UUID directory and
synthetic SQLite ledger, ignoring ambient application/SQLite settings and .env.
A successful diagnostic is **not live integration PASS**.

Offline (no paid request):

```powershell
python -m trustflow.evaluation.diagnose --offline --profile spark --max-requests 1 --retries 0 --max-cost-usd 0.03 --max-output-tokens 4096 --reasoning-effort low --out artifacts/m53-diagnostic-offline
```

**Only after separate explicit owner authorization**, with the credential already
provided by the environment, the proposed ONE-REQUEST paid command is:

```powershell
python -m trustflow.evaluation.diagnose --live --authorize-paid --profile spark --max-requests 1 --retries 0 --max-cost-usd 0.03 --max-output-tokens 4096 --reasoning-effort low --out artifacts/m53-diagnostic-live
```

The CLI rejects non-Spark profiles, retries, more than one HTTP attempt, effort
outside `low|minimal`, output caps above 4096 and estimated budgets above $0.05.
The recommended budget is $0.03; reservation uses serialized request/schema input
estimate plus the full 4096-token output allowance. It is an advisory estimate,
not a provider billing ceiling. No API key is placed in the command or report.
Default invocation (neither `--live` nor `--offline`) prints a zero-request plan.
The diagnostic does not run the five-case matrix or use foreign resources.

Interpret the new evidence before selecting another request: explicit
`incomplete/max_output_tokens` establishes output-limit exhaustion for **that**
request; the reasoning count identifies its share. A completed response with
`json_syntax` instead points to malformed text; output item metadata distinguishes
missing/unsupported/ambiguous messages. Do not relax the parser or schema based on
the original generic error. Once an actual server shape is observed, add a
sanitized offline fixture preserving that shape; current fixtures represent the
documented protocol and hypothetical limit exhaustion only.

База: принятый M5.2 `38cc3942dbd715681d59bfd893e47643210294a9`.
Ветка: `m5.3/meta-live-ready`. Это developer implementation/evidence,
а не независимое acceptance. Платных запросов при реализации не выполнялось.
Offline MockTransport PASS не подтверждает успешную live-интеграцию.

## Проверенный официальный контракт

Документация прочитана 2026-11-01 (Asia/Qyzylorda):

| Источник | Подтверждено документацией | Реализация / предел проверки |
|---|---|---|
| [Responses API](https://dev.meta.ai/docs/protocols/responses) | `POST https://api.meta.ai/v1/responses`; `model`, `input`, `max_output_tokens`; assistant message / `output_text`; `usage.input_tokens` / `output_tokens`; `store` по умолчанию true; cap включает reasoning | raw httpx, `store:false`, отдельные stateless planning/review calls; incomplete/failed/refusal/ambiguous output запрещены; reasoning не используется как ответ |
| [Structured output](https://dev.meta.ai/docs/structured-output) | Responses использует `text.format`, а не Chat Completions `response_format`; strict по умолчанию false; strict требует object root, все properties в required и `additionalProperties:false`; циклические `$ref` запрещены | Оба плоских schema совместимы с описанным subset; теперь `strict:true`. Прежний `strict:false` был разрешён API, это усиление проверки, а не доказанная несовместимость. Внутренний schema checker намеренно поддерживает только используемый TrustFlow subset |
| [Models](https://dev.meta.ai/docs/models) | `muse-spark-1.3` и `muse-wonder-1.0` доступны через Responses; Wonder Full — отдельная модель, не доступная через Model API | allowlist этих двух Standard IDs; planner и reviewer настраиваются независимо. Contributor/Full автоматически не выбираются. Account access и фактическая доступность не проверены |
| [Errors](https://dev.meta.ai/docs/error-handling) | HTTP 400/401/402/403/404/413 требуют исправления причины; 429 и transient 5xx допускают backoff; 429 сообщает Retry-After | всего `1 + TRUSTFLOW_LLM_MAX_RETRIES` попыток (0–3 retries), transient set 429/500/502/503/504; transport timeout тоже ограничен. Числовой cooldown >10 s завершает вызов без раннего retry. Raw body/exception не публикуются |

Это проверка реализации по официальной спецификации и офлайн wire fixtures.
Серверное принятие схем, доступ аккаунта и реальные ответы обоих Muse IDs остаются
открытыми live-gates. `reasoning.effort`, sampling, streaming, background,
server-side tools и encrypted reasoning replay здесь не используются.
План — JSON-предложение локальных инструментов, а не разрешение на их исполнение.

## Защитные границы

- FakeLLMProvider и `docker-compose.yml` сохраняют прежний default offline demo.
  Live не переключается на fake при ошибке.
- Ключ берётся только из process environment `MODEL_API_KEY`.
  `TRUSTFLOW_LLM_API_KEY`, `META_API_KEY` и значение MODEL_API_KEY в Python `.env`
  не авторизуют приложение. Legacy constructor field игнорируется и исключён из repr/dump.
- `MockTransport` всегда получает placeholder, даже если у разработчика задан ключ.
  Unit tests блокируют внешний httpx transport. В evidence-процессы реальные
  credential variables не передаются.
- Для настоящего transport разрешён только `https://api.meta.ai/v1`;
  redirects не следуются, environment proxy settings отключены (`trust_env=False`).
- Каждый план проходит прежние allowlist, Pydantic args и preconditions до исполнения.
  Некорректный typed plan получает максимум один repair; malformed JSON завершает
  planning без исполнения. Semantic output проходит отдельную обязательную typed
  проверку, включая запрет лишних полей и нечислового confidence.
- Backend PolicyEngine, hash/snapshot approvals, backend-owned operation IDs,
  idempotent ledger и verification-only recovery сохранены. Модель не получает
  права менять risk, approval, intent identity или источник ledger evidence.
- Логи/artifacts не содержат raw HTTP requests/responses, headers, reasoning или
  environment dumps. IDs ограничены форматом и длиной; отражённые credentials
  отбрасываются. Rationale/review strings и текстовые artifact values санитизируются.
- `api/factory.py` содержит прежнюю фабрику API без eager startup; ASGI entrypoint
  `trustflow.api.app:app` сохраняет startup и fail-closed поведение. Harness
  импортирует только фабрику и использует `EvaluationSettings` из явных значений,
  чтобы plan-only не открывал обычную demo-БД, а .env/environment не меняли его
  synthetic persistence или policy threshold.

## Лимиты

| Настройка | Default приложения | Live Compose | Smoke |
|---|---:|---:|---:|
| HTTP attempts | 24 | 12 | `--max-requests 10` |
| estimated USD | 0.50 | 0.25 | `--max-cost-usd 0.25` |
| transport retries | 2 | 0 | `--retries 0` |
| timeout, s | 30 | 30 | 30 |
| output cap, tokens | 1200 | 1200 | `--max-output-tokens 1200` |

`LiveCallBudget` атомарно резервирует count и estimated cost **до каждой**
HTTP-попытки, включая retries и failed calls. Одна budget instance общая для
planner/reviewer; matrix harness использует её для всех выбранных profiles.
Input estimate включает сериализованный request/schema плюс overhead. При известном
usage reservation заменяется фактической оценкой; при неизвестном удерживается
не меньше reservation и известной части стоимости. Учёт ведётся и без workflow tracker.

Эти process/session limits сбрасываются при новом процессе или экземпляре provider.
Они не являются account-wide квотой. Per-workflow token usage и semantic attempt
limits по-прежнему сохраняются в SQLite при restart; failed attempts не исчезают.
Неизвестные tokens остаются `null`; нельзя считать их нулевой стоимостью.
Поздно полученный usage может превысить оценку; cached input считается по полной
ставке. Application-side limits **advisory и не гарантируют точный billing ceiling**.
Превышение закрывает последующие HTTP attempts, но не отменяет уже понесённых расходов.

## Команды без платного inference

Подготовка нового checkout при необходимости:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,demo]"
```

План без ключа, без HTTP и без БД:

```powershell
.\.venv\Scripts\python.exe -m trustflow.evaluation.live --profile all
```

Проверка harness с MockTransport и настоящими локальными API/SQLite contracts:

```powershell
.\.venv\Scripts\python.exe -m trustflow.evaluation.live --offline --profile all --max-requests 27 --max-cost-usd 1 --out artifacts/m53-mock
.\.venv\Scripts\python.exe -m pytest tests/unit/test_m53_meta.py tests/unit/test_m53_harness.py -q
```

По умолчанию CLI не выполняет inference. `--live` без `--authorize-paid`,
обратная комбинация и `--offline --authorize-paid` запрещены до вызовов.
Флаг фиксирует заявление оператора о разрешении; отдельное разрешение пользователя
вне CLI всё равно необходимо.

## Отдельный bounded smoke — только после разрешения

Ключ уже должен быть безопасно установлен оператором в process environment.
Команды чтения/вывода ключа и запись ключа в файлы не требуются.

```powershell
.\.venv\Scripts\python.exe -m trustflow.evaluation.live --live --authorize-paid --profile spark --max-requests 10 --max-cost-usd 0.25 --max-output-tokens 1200 --retries 0 --out artifacts/m53-live
```

Один конечный набор, без фоновых запусков и открытых batches. Exit 0 означает
выполненные assertions выбранного набора, 1 — несовпадение/исчерпание лимитов,
2 — invalid CLI или interrupted harness; `completed` и `passed` различаются.
Каждый run создаёт новый UUID directory и synthetic SQLite. Существующие runs
не перезаписываются. `cases.json` сохраняется по мере выполнения; `report.json`
содержит session counters и при исключении, `FAILED.json` отмечает interruption.

После отдельно согласованного Spark smoke можно отдельно разрешить `--profile wonder`
или `--profile mixed`; для примера Wonder нужны иные согласованные cost limits.
`--profile all --max-requests 30 --max-cost-usd 1` — конечная общая matrix,
не автоматическое продолжение предыдущего smoke и не разрешение на её запуск.

## Локальный live UI

Standalone `docker-compose.live.yml` использует проект `trustflow-m53-live`,
ports `127.0.0.1:18000:8000` / `127.0.0.1:13000:3000`, отдельный volume
`m53-synthetic:/synthetic`. Fake Compose не подключается как base/override.

Только после разрешения использовать UI для платных целей:

```powershell
docker compose --env-file NUL -f docker-compose.live.yml up --build -d
# http://127.0.0.1:13000 ; approval token: m53-synthetic-local-approval
docker compose --env-file NUL -f docker-compose.live.yml down
```

`NUL` — Windows null env file, исключающий .env interpolation; на Unix используйте
`/dev/null`. Startup не вызывает inference; отправка goal вызывает.
Не включайте `compose config`/`inspect Config.Env` с реальным ключом в evidence.
Demo approval token публичный, предназначен только для loopback synthetic sandbox.
Обычный `down` сохраняет live SQLite; очистка volume — отдельное явное действие.
Эта конфигурация проверена через rendered config, но настоящий live stack не запускался.

## Семантические задачи и измерения

| Case | Проверяемое поведение | Вклад inference |
|---|---|---|
| read | balance acc_alice, COMPLETED, ledger без переводов | построение typed tool plan и независимый semantic review |
| approve | ровно 1500 USD Alice→Bob; wrong hash 409; approval replay 409; один ledger tx | планирование; review реального ledger после exact-hash approval |
| reject | HIGH 3000 USD, REJECTED, без нового tx | планирование; решение reject принадлежит оператору |
| recovery | HIGH 1200 USD; после первого review внедрён invalid verdict; VERIFICATION_PENDING→verify-retry→COMPLETED, tx неизменен | оба review действительно вызывают выбранную модель в live mode; повреждение первого результата явно synthetic |
| semantic_negative | цель 1700 USD против одобренного и исполненного 1500 USD; expected FAIL; ledger неизменен | независимое обнаружение semantic mismatch, не подмена deterministic verifier |

HIGH approvals в harness автоматизированы только для явно заданных synthetic fixture
payloads после проверки суммы/accounts/currency и точного hash через настоящий API.
Это тест пути human approval, а не свидетельство интерактивного решения человека.
Любой другой proposed payment не одобряется. Проверка ledger выполняется отдельной
read-only sqlite3 connection, не из executor claims. Safety mismatch останавливает profile.

Обычный проход требует 4 planning + 5 review = 9 HTTP attempts/profile без repairs.
Общий cap может закончить набор раньше — это FAIL, а не сокращённый PASS.
На эквивалентных целях запускается неизменённый FakeLLMProvider; сравниваются
семантика, статусы, approvals, tx effects, а не тексты, UUID или byte-identical JSON.
У fake ожидаемо 4/5: его прежний heuristic reviewer не замечает negative amount mismatch.
Это известная ограниченность fake comparator, не результат измерения Muse.

`report.json` содержит actual requested model ID, request ID (HTTP header,
иначе явно допустимый response-ID fallback), отдельный response ID, workflow ID,
purpose/logical/transport attempt, HTTP status, input/output tokens, latency,
estimated cost, budget charge, typed validation outcome и failure reason.
`model_metrics` агрегирует schema validity, latency и cost по моделям; injected
invalid output выделен отдельно. При неизвестном usage total cost остаётся null,
известная часть показана отдельно. SQLite сохраняет фактические committed operations.

## Ожидаемые расходы и выбор модели

Официальные [Standard token rates](https://dev.meta.ai/docs/pricing-rate-limits):
Spark input/output $1.25/$4.25, Wonder $4/$20 за 1M tokens.
Это не измеренный расход; тарифы нужно повторно проверить перед платным запуском.

Оценка при **1500 input + 1200 output tokens на каждый вызов**, без cache/retries/repairs:

| Profile | Planning / review | 9 calls, USD |
|---|---|---:|
| spark | Spark / Spark | 0.062775 |
| wonder | Wonder / Wonder | 0.270000 |
| mixed | Spark / Wonder | 0.177900 |
| все три | 27 calls | 0.510675 |

Формула: `(input_tokens × input_rate + output_tokens × output_rate) / 1_000_000`.
Пример не обещает качество, достаточность output cap или billing ceiling.
Выбор после live runs: сначала task completion и valid schema rate, затем latency
и total estimated cost; mixed полезен только если измерения оправдывают более дорогой review.
Wonder не объявляется лучшим без результатов. Отказы, incomplete и budget failures
считаются результатами оценки, а не исключаются из сравнения.

## Демонстрация и independent handoff

Короткий показ reviewer:

1. Выполнить plan-only command и показать отсутствие платных вызовов.
2. Для offline rehearsal открыть `mode:offline_mock`; для настоящего показа нужен
   отдельно разрешённый smoke с `mode:live`, clean candidate SHA и actual call IDs/tokens.
3. В `approve` показать Muse-proposed typed steps, backend HIGH, exact approved hash,
   wrong/replayed approval 409 и одну отдельную SQLite transaction.
4. Показать отдельный `semantic_review` request выбранной модели и negative case.
   Затем recovery: injected invalid output, новая verification call, тот же tx/operation.
5. Сопоставить task outcomes/cost/latency Spark, Wonder, mixed и fake, сохраняя FAIL cases.

Для независимого acceptance проверить exact candidate/range и clean Git, повторить
offline/real Docker gates, прочитать adapters и ledger evidence; developer report
не является доказательством сам по себе. Открытые live-gates: account access,
серверное принятие strict schemas, реальные full task results/usage/latency/cost
для обоих IDs и mixed. Закрыть их может только отдельно разрешённое исполнение.
Полное M5.3 acceptance пока не заявляется; push/merge/tag/deploy не выполняются.
