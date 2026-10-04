# Установка и обновление на Ubuntu

Инструкция относится к штатному `docker-compose.yml`: приложение `app`,
PostgreSQL 16 в сервисе `db`, база и пользователь `nvrmon`, веб-порт 8000.
При другой схеме запуска сначала уточните её; не создавайте рядом второй экземпляр
мониторинга с отдельной базой и планировщиком.

Ветка обновления — `codex/universal-snmp-monitoring`. Команды получения этой ветки
можно выполнять **после её публикации в GitHub**. Если Git сообщает, что ветки нет,
остановитесь: переключение и перезапуск пока не нужны.

Сборка включает изменения из `codex/ntp-time-sync` (до `ae54b87` включительно).
При переходе с этой ветки сохраняются источник NTP, расписание и настройки
в существующей базе. На дашборде остаётся кнопка «Настройки времени».

## 1. Подключение и проверка сервера

Используйте SSH. Если под «телнетом» имеется в виду PuTTY, выберите в нём тип
соединения SSH и настроенный на сервере порт (обычно 22). Настоящий Telnet
не шифрует соединение. Из терминала компьютера:

```bash
ssh ИМЯ_ПОЛЬЗОВАТЕЛЯ@IP_СЕРВЕРА
```

На сервере проверьте установленную систему и способ запуска:

```bash
cat /etc/os-release
pwd
sudo docker compose version
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
```

Если Docker отсутствует, а панель уже работает, сначала найдите её текущий сервис.
Для уточнения достаточно вывода этих команд; содержимое `.env` и пароли не нужны.

## 2. Обновление уже работающей панели

Перейдите в **существующий** каталог проекта, в котором лежит используемый
`docker-compose.yml`. Сохраните прежнее имя проекта Compose, дополнительные
`-f`/`--env-file`/`-p`, если запускали с ними. Смена имени проекта может подключить
новый пустой том вместо существующей базы.

```bash
cd /ПУТЬ/К/СУЩЕСТВУЮЩЕМУ/ПРОЕКТУ
git status --short
sudo docker compose ps
```

Если `git status` показывает изменения, сначала разберите их. Не используйте
`git reset --hard` и не заменяйте `.env` файлом из примера. Если состав сервисов
или база отличаются от описания выше, команды резервирования нужно адаптировать.

Следующий блок выполняется целиком из каталога проекта. Он останавливает
приложение на время резервирования и сборки; база остаётся запущенной.
При ошибке блок прекращает работу. Сохраните показанный путь резервной копии.

```bash
(
  set -eu
  test -z "$(git status --porcelain)"
  git fetch origin refs/heads/codex/universal-snmp-monitoring
  SNMP_UPDATE_COMMIT="$(git rev-parse FETCH_HEAD)"

  umask 077
  SNMP_BACKUP_DIR="$HOME/nvr-backup-$(date +%Y%m%d-%H%M%S)"
  mkdir -m 700 "$SNMP_BACKUP_DIR"
  git rev-parse HEAD > "$SNMP_BACKUP_DIR/commit-before.txt"
  printf 'Резервная копия: %s\n' "$SNMP_BACKUP_DIR"

  sudo docker compose stop app
  sudo tar -czf "$SNMP_BACKUP_DIR/config-data.tar.gz" .env docker-compose.yml data
  sudo docker compose exec -T db pg_dump -U nvrmon -d nvrmon -Fc \
    > "$SNMP_BACKUP_DIR/database.dump"
  sudo docker compose exec -T db pg_restore --list \
    < "$SNMP_BACKUP_DIR/database.dump" > /dev/null

  git switch --detach "$SNMP_UPDATE_COMMIT"
  sudo docker compose up -d --build --wait --wait-timeout 180 app
)
```

В этой схеме копируются база, `.env`, основной Compose-файл и `data`, включая
`data/secret.key`, если ключ хранится там. Проверка списка дампа подтверждает,
что архив читается; полноценную проверку восстановления выполняют на отдельной БД.
Дополнительные Compose-файлы и внешние ключи сохраните тоже, если используете их.
Каталог клипов `clips` обновление не изменяет; его обычное резервирование ведётся
отдельно. Резервную копию скопируйте в своё защищённое хранилище.

Новая зависимость `pysnmp` устанавливается при сборке. Одного `restart app`
для этого обновления недостаточно. При старте приложение само добавит таблицы
и колонки. Существующие `SECRET_KEY` и `NVR_SECRET_KEY` **не меняйте**.

Если блок завершился ошибкой, не переходите к следующим шагам вслепую:
приложение может остаться остановленным. Если ошибка произошла до переключения
кода, прежний контейнер можно вернуть командой `sudo docker compose start app`.
После переключения сначала разберите ошибку и состояние миграции. Для полного
отката нужны прежний коммит, его зависимости и соответствующая резервная копия БД;
восстановление старой БД удалит изменения данных, сделанные после резервирования.
Не удаляйте тома командой `docker compose down -v`.

## 3. Установка на пустой сервер

Этот раздел используется только если прежней установки нет. Установите Docker
Engine и Compose plugin по [официальной инструкции для Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
Она использует репозиторий Docker; не смешивайте пакеты из разных источников
на уже работающем сервере. Для загрузки проекта также нужен Git.

```bash
sudo apt update
sudo apt install -y git
git clone --branch codex/universal-snmp-monitoring --single-branch https://github.com/IOOgit/newapp.git nvrmon
cd nvrmon
umask 077
cp .env.example .env
chmod 600 .env
sudo docker compose build app
sudo docker compose run --rm --no-deps app python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
nano .env
```

В `.env` задайте `SECRET_KEY` сгенерированным значением, `OWNER_USERNAME` — своим
логином, `OWNER_PASSWORD` — сильным паролем для первого входа. Для регистраторов
в часовом поясе Владивостока задайте `TIMEZONE=Asia/Vladivostok` и
`TZ=Asia/Vladivostok`; для других регистраторов используйте их пояс.
`MOCK_MODE` оставьте `false`. Остальные SNMP-настройки имеют значения по умолчанию.

В штатном Compose `DATABASE_URL` задан непосредственно у сервиса `app` и имеет
приоритет над строкой SQLite из `.env`. Это установка с PostgreSQL.

```bash
sudo docker compose up -d --wait --wait-timeout 180 app
```

Откройте `http://IP_СЕРВЕРА:8000` по локальной сети/VPN и войдите с указанными
`OWNER_USERNAME` и `OWNER_PASSWORD`. Затем очистите только значение
`OWNER_PASSWORD` в `.env` и примените окружение:

```bash
sudo docker compose up -d app
```

Пароль остаётся в БД. Непустой `OWNER_PASSWORD` заново устанавливает его при старте.

Для первого доступа через публичный сервер можно изменить публикацию порта `app`
в Compose на `"127.0.0.1:8000:8000"` **до запуска** и использовать SSH-туннель
с компьютера: `ssh -L 18000:127.0.0.1:8000 ИМЯ_ПОЛЬЗОВАТЕЛЯ@IP_СЕРВЕРА`.
Панель тогда открывается по `http://127.0.0.1:18000`. Для постоянного доступа
по домену в проекте есть Caddy: задайте `DOMAIN`, настройте DNS и порты 80/443,
запустите `sudo docker compose --profile https up -d`. При таком доступе
публикацию порта 8000 также ограничьте loopback или уберите: Caddy обращается
к `app` внутри сети Compose. Публикуемые Docker-порты могут обходить правила UFW.

## 4. Проверка запуска и SNMP

```bash
sudo docker compose ps
sudo docker compose logs --tail=80 app
curl -fsS http://127.0.0.1:8000/healthz
```

Команда `curl` выше подходит при сохранённой публикации порта 8000 на хосте.
Если порт убран для работы только через Caddy, проверьте `/healthz` через свой HTTPS-домен.

После обновления откройте «Коммутаторы», добавьте адрес, включите SNMP v2c,
укажите UDP-порт 161 и read-only community, нажмите «Проверить SNMP» и сохраните.
На самом коммутаторе SNMP должен быть включён, а его ACL — разрешать адрес,
с которого приходит сервер. Нужен маршрут из контейнера к коммутатору через
локальную сеть или VPN и разрешённый исходящий UDP 161 с ответным трафиком.
Для обычного опроса входящий порт 161 на сервере публиковать не требуется.
Трафик появится после двух успешных измерений; недоступные показатели будут «—».

Полный набор автоматических тестов проверен локально. Развёртывание на вашем
сервере и опрос реального DES-1210-28P ещё не выполнялись.

Источники: [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/),
[pg_dump PostgreSQL 16](https://www.postgresql.org/docs/16/app-pgdump.html),
[OpenSSH на Ubuntu](https://documentation.ubuntu.com/server/how-to/security/openssh-server/),
[Docker и межсетевые экраны](https://docs.docker.com/engine/network/packet-filtering-firewalls/).
