# Посредник GigaChat (Supabase Edge Function)

Ключ Сбера хранится только в секретах проекта Supabase. Программа ходит сюда с личным кодом доступа.

Секреты (Project → Edge Functions → Secrets):

| Секрет | Что это |
|---|---|
| `GIGACHAT_AUTH_KEY` | ключ авторизации из кабинета Сбера |
| `GIGACHAT_SCOPE` | необязательно, по умолчанию `GIGACHAT_API_PERS` |
| `SALUTE_AUTH_KEY` | ключ SaluteSpeech из кабинета Сбера; сервер выдаёт программе только временный пропуск, звук через сервер не идёт |
| `SALUTE_SCOPE` | необязательно, по умолчанию `SALUTE_SPEECH_PERS` |
| `SBER_CA_PEM` | корневой сертификат Минцифры (весь текст .crt), нужен для TLS к серверам Сбера |
| `TEAM_TOKENS` | коды доступа: `Валентин:код1,Иван:код2` (код от 12 символов) |

Отозвать доступ сотруднику — убрать его код из `TEAM_TOKENS`. Развёртывание: `supabase functions deploy giga-proxy --no-verify-jwt`.
