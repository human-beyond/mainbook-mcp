# Отчёт: этап 4.5 — OAuth-проверяльщик MCP и права по инструментам

Дата: 2026-08-19  
Репозиторий: `mainbook-mcp`  
Ветка: `feature/oauth-verifier` от `main` (`d6fac5e`)  
Основной коммит реализации: `d716510`

## Что сделано

1. Добавлен локальный проверяльщик MainBook access JWT в
   `src/mainbook_mcp/oauth_verifier.py`:
   - максимальный размер токена — 8192 байта;
   - разрешён только `RS256`; `none`, HMAC и другие алгоритмы закрыты до проверки подписи;
   - `kid` ищется только в JWKS по настроенному URL; `jku`/`x5u` из токена не используются;
   - JWKS ограничен 256 KiB и 32 ключами, кэшируется на 300 секунд;
   - неизвестный `kid` может вызвать одно обновление, но не чаще раза в 30 секунд;
   - подпись, точные `iss` и строковый `aud`, `exp`/`iat`/опциональный `nbf`, допуск часов,
     максимальный возраст и максимальный срок токена проверяются fail-closed;
   - `sub` обязан быть UUID; обязательны непустые `jti`, `client_id`, `consent_id` и строковый
     `scope`.
2. Добавлена ленивая HTTP-аутентификация в `src/mainbook_mcp/oauth_http.py`:
   - `initialize` и `tools/list` остаются анонимными;
   - проверка выполняется до входа в обработчик `tools/call`;
   - `mb_live_` идёт по прежнему пути;
   - всё остальное проверяется как OAuth JWT;
   - один словарь `TOOL_SCOPES` связывает четыре hosted-инструмента с областями;
   - недостаточная область останавливает запрос до обработчика и побочных эффектов;
   - все ошибки удостоверения дают одинаковые тело и `WWW-Authenticate` в HTTP 401;
   - недостаточная область даёт HTTP 403 и стабильное тело `insufficient_scope`;
   - внутренний 401 служебной двери (например, неактивный пользователь; в 4.6 — отозванное
     согласие) преобразуется в тот же внешний непрозрачный HTTP 401.
3. При включённом флаге публикуется
   `/.well-known/oauth-protected-resource/mcp` с ресурсом
   `https://mcp.mainbook.ai/mcp`, authorization server `https://api.mainbook.ai`, методом Bearer
   `header` и двумя поддерживаемыми областями. При выключенном флаге маршрут отсутствует.
4. Для проверенного OAuth-вызова `MainBookClient` выпускает отдельный HS256 JWT на каждый REST-
   запрос к Django:
   - заголовок — только `X-MainBook-Service`;
   - `iss=mcp`, `aud=api.mainbook.ai`, UUID `sub`, `cid`, уникальный `jti`, `exp-iat=60`;
   - исходный клиентский Bearer не передаётся в Developer API ни в одном заголовке;
   - presigned upload по-прежнему не получает ни одного удостоверения MainBook.
5. Добавлен флаг `MAINBOOK_MCP_OAUTH_ENABLED`, по умолчанию `false`, и настройки issuer, JWKS,
   resource, clock skew, max token age, JWKS TTL/refresh interval и
   `MCP_SERVICE_SIGNING_SECRETS`. При выключенном флаге даже ошибочные неактивные OAuth-настройки
   не влияют на прежний запуск.
6. Обновлены `README.md` и `CHANGELOG.md` как dark launch: прямо сказано, что код не опубликован,
   не развёрнут и не означает, что вход уже работает на hosted-сервисе.

## Решения, принятые исполнителем

- Встроенный общий auth middleware SDK не использован: он закрыл бы также `initialize` и
  `tools/list`, нарушив Р1. Сделана узкая ASGI-обёртка только для JSON-RPC `tools/call`.
- После проверки во внутренний request scope добавляется служебная, предварительно очищенная от
  пользовательской подмены метка `sub + client_id`. Она не содержит access token. Это позволяет
  обработчику выпустить служебное удостоверение, не проверяя JWT второй раз.
- Служебный JWT выпускается не один раз на MCP-вызов, а заново на каждый запрос к Developer API:
  Django атомарно гасит `jti`, поэтому повторное использование одного JWT сломало бы инструменты,
  выполняющие несколько REST-запросов.
- Значения по умолчанию согласованы с backend-контрактом: token age/lifetime 600 секунд, clock skew
  5 секунд, JWKS cache 300 секунд; минимальный интервал аварийного refresh — 30 секунд.
- Добавлена прямая runtime-зависимость `PyJWT[crypto]>=2.10,<3`. Проверенные лицензии окружения:
  PyJWT 2.13.0 — MIT; cryptography 50.0.0 — Apache-2.0 OR BSD-3-Clause. GPL/AGPL нет.

## Что не сделано и почему

- Не реализована онлайн-проверка активного consent/token marker и настоящий отзыв доступа — это
  этап 4.6. Граница MCP уже превращает будущий 401 этой проверки в требуемый непрозрачный 401.
- Не реализирована регистрация клиентов по metadata — этап 4.7.
- Код backend не менялся. Прочитаны его контракты, JWT/JWKS evidence и служебная дверь; разрешение
  трогать backend-документацию не понадобилось.
- Не выполнялись публикация PyPI/registry, push, merge в `main` и deployment DigitalOcean.
- Live production gate не запускался: этап запрещает выкатывание, а все новые проверки полностью
  детерминированы локальными ключами, ASGI/HTTP mock и локальным Streamable HTTP сервером.

## Спорные места

- `MCP_OAUTH_CONTRACTS.md`, контракт 1, содержит более старое решение закрыть `initialize` и
  `tools/list`. Р1 этой задачи явно отменяет его из-за Glama/punkpeye. Реализация следует Р1;
  отдельный настоящий HTTP-тест фиксирует анонимный handshake и список инструментов.
- Других молчаливых отклонений от закрытых развилок нет.

## Тесты и проверки

Финальный полный прогон:

```text
.venv/bin/pytest --cov=mainbook_mcp --cov-report=term-missing
collected 258 items
257 passed, 1 skipped in 3.55s
TOTAL coverage: 91.05% (порог 90% выполнен)
```

Пропущен только существующий `tests/test_files.py:171`: проверка разных имён каталогов, которые
отличаются только регистром, требует case-sensitive filesystem.

```text
.venv/bin/ruff check .
All checks passed!

git diff --check
ошибок нет
```

Сборка:

```text
uv build --out-dir /tmp/mainbook-mcp-oauth-final-dist-20260819
Successfully built mainbook_mcp-0.5.1.tar.gz
Successfully built mainbook_mcp-0.5.1-py3-none-any.whl
```

Проверено содержимое обоих артефактов: `oauth_http.py` и `oauth_verifier.py` присутствуют; metadata
wheel содержит `Requires-Dist: pyjwt[crypto]<3,>=2.10`. Дубликатов вида `имя 2.py` нет.

Новая негативная матрица включает: `none`, HMAC с публичным RSA-ключом, чужой алгоритм, неверную
подпись, неизвестный `kid` и один refresh, запрет URL из токена, previous-key overlap, чужие
`iss`/`aud`, slash mismatch, list audience, expiry/future/max-age/max-lifetime/`nbf`, не-UUID и
отсутствующие claims, карту scopes, read-only отказ конвертации до обработчика, попарно одинаковые
401, реальный внешний 401 после внутреннего service-door 401, отсутствие token/secret/code в логах,
анонимные initialize/list, legacy key с обоими значениями флага и перехваченный внутренний запрос
без клиентского Bearer.

## Платные API

Платные API не вызывались: 0 вызовов DataForSEO, Exa, Firecrawl, ScrapeCreators, Apify и других
платных методов. Примерная сумма: **$0.00**. Использовались только локальные тесты, локальная сборка
и скачивание open-source Python-зависимостей через package manager.

## `git log --oneline -5`

Снимок сделан после основного коммита реализации и до отдельного коммита этого отчёта:

```text
d716510 (HEAD -> feature/oauth-verifier) Add hosted OAuth token verifier and tool scopes
d6fac5e (tag: v0.5.1, origin/main, origin/HEAD, main) Name the maintainer so the listing can be claimed
d4e6bcd (feature/hosted-mode) Serve four tools over HTTP, five over stdio, and name them all
874472c (tag: v0.5.0, origin/feature/cli-auth-login, feature/cli-auth-login) Fix terminal auth origin and credential lifecycle
5637846 feat: add browser-assisted terminal login
```
