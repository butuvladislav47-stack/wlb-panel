# WLB Panel alpha 0.0.2

Веб-панель для Ubuntu-сервера, которая запускает и поддерживает отдельные
процессы `headless-wbstream-creator`.

## Файлы для GitHub

Загрузи эти файлы именно в корень ветки `main`:

- `install.sh`
- `panel.py`
- `README.md`

## Установка или обновление

Запусти на сервере:

```bash
wget -O /tmp/wlb-install.sh https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main/install.sh && sudo bash /tmp/wlb-install.sh
```

Повторная установка сохраняет пароль панели и cookies. Старые звонки и ссылки
удаляются: после установки создай свежую ссылку.

После установки будут показаны:

- адрес панели на порту `8088`;
- логин и пароль панели;
- пароль noVNC;
- команды диагностики.

## Использование

1. Открой панель на порту `8088`.
2. Перейди в `WB Login Browser` и запусти серверный браузер.
3. Подключись к noVNC с паролем, показанным установщиком и в панели.
4. Открой `stream.wb.ru`, войди в аккаунт и обнови страницу.
5. Нажми `Импортировать cookies из серверного Chrome`.
6. Создай именованную WB Stream ссылку на главной странице.
7. На телефоне удали старую сохранённую ссылку, добавь новую и нажми Connect.

Если Android показывает `Previous session is still shutting down`, полностью
закрой приложение, удали старый звонок из списка и добавь свежую ссылку.

Панель читает WB device ID из `localStorage` и сохраняет его как обязательную
cookie `__wb_device_id`.

Созданная ссылка работает, пока на сервере запущен соответствующий creator.
Старые комнаты намеренно не восстанавливаются: повторный вход creator в комнату
может оставить Android-клиент в состоянии `Previous session is still shutting down`.

## Диагностика

```bash
systemctl status wlb-panel
journalctl -u wlb-panel -n 100 --no-pager
ss -lntp | grep -E '8088|6080'
```

## DNS

The installer builds a server-side DNS redirect into the creator. The Android
app can stay on `DNS: System`; DNS traffic sent to a local router address is
redirected by the server to `1.1.1.1`.

Порты `8088/tcp` и `6080/tcp` должны быть разрешены в firewall сервера и
панели управления хостингом.
