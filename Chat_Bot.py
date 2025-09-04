import asyncio
import socks
import socket
import logging
import sys

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    handlers=[
        logging.FileHandler("twitch_bot.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("TwitchBot")

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6667  # plain IRC; если нужен TLS, менять логику

class TwitchBot:
    def __init__(self, username, oauth_token, proxy=None, channel='your_channel'):
        self.username = username
        self.oauth_token = oauth_token
        self.proxy = proxy  # "ip:port" or None
        self.channel = channel.lower().lstrip('#')
        self.reader = None
        self.writer = None
        self.connected = False
        self._stop = False
        self.listen_task = None

    async def connect(self):
        if self.connected:
            return
        loop = asyncio.get_event_loop()
        try:
            if self.proxy:
                proxy_ip, proxy_port = self.proxy.split(':')
                proxy_port = int(proxy_port)
                sock = socks.socksocket()
                sock.set_proxy(socks.SOCKS5, proxy_ip, proxy_port)
                sock.setblocking(False)
                # Асинхронное подключение сокета
                await loop.sock_connect(sock, (IRC_HOST, IRC_PORT))
                # Получаем reader/writer из уже подключенного сокета
                self.reader, self.writer = await asyncio.open_connection(sock=sock)
            else:
                self.reader, self.writer = await asyncio.open_connection(IRC_HOST, IRC_PORT)

            # Авторизация
            self.writer.write(f"PASS {self.oauth_token}\r\n".encode('utf-8'))
            self.writer.write(f"NICK {self.username}\r\n".encode('utf-8'))
            self.writer.write(f"JOIN #{self.channel}\r\n".encode('utf-8'))
            await self.writer.drain()

            self.connected = True
            logger.info(f"{self.username} connected to #{self.channel} ({'proxy='+str(self.proxy) if self.proxy else 'direct'})")

            # Запускаем слушатель
            if not self.listen_task or self.listen_task.done():
                self.listen_task = asyncio.create_task(self.listen())
        except Exception as e:
            logger.error(f"{self.username} connection error: {e}")
            self.connected = False
            # Не пробрасываем исключение вверх — будем пытаться переподключаться централизованно
            raise

    async def close(self):
        self._stop = True
        if self.listen_task:
            self.listen_task.cancel()
            try:
                await self.listen_task
            except asyncio.CancelledError:
                pass
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.connected = False

    async def listen(self):
        try:
            while not self._stop:
                line = await self.reader.readline()
                if not line:
                    logger.warning(f"{self.username} connection closed by server")
                    self.connected = False
                    break
                decoded = line.decode('utf-8', errors='ignore').strip()
                logger.debug(f"{self.username} received: {decoded}")
                if decoded.startswith("PING"):
                    pong = decoded.replace("PING", "PONG")
                    try:
                        self.writer.write(f"{pong}\r\n".encode('utf-8'))
                        await self.writer.drain()
                        logger.debug(f"{self.username} sent: {pong}")
                    except Exception as e:
                        logger.error(f"{self.username} failed to send PONG: {e}")
                        self.connected = False
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"{self.username} listen error: {e}")
            self.connected = False

    async def ensure_connected(self, reconnect_delay=5, max_attempts=3):
        """Попытаться подключиться, если не подключён"""
        attempts = 0
        while not self.connected and not self._stop and attempts < max_attempts:
            attempts += 1
            try:
                await self.connect()
            except Exception:
                logger.info(f"{self.username} reconnect attempt {attempts} failed, waiting {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
        return self.connected

    async def send_message(self, message):
        if not self.connected:
            raise RuntimeError(f"{self.username} not connected")
        try:
            msg = f"PRIVMSG #{self.channel} :{message}\r\n"
            self.writer.write(msg.encode('utf-8'))
            await self.writer.drain()
            logger.info(f"{self.username} sent: {message}")
        except Exception as e:
            logger.error(f"{self.username} error sending message: {e}")
            self.connected = False
            raise


async def main():
    # --- Load accounts ---
    accounts = []
    try:
        with open('accounts.txt', 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or ';' not in line:
                    continue
                username, oauth = line.split(';', 1)
                accounts.append((username.strip(), oauth.strip()))
    except FileNotFoundError:
        logger.error("accounts.txt not found")
        return

    if not accounts:
        logger.error("Нет аккаунтов в accounts.txt")
        return

    # --- Load messages ---
    try:
        with open('messages.txt', 'r', encoding='utf-8') as f:
            messages = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logger.warning("messages.txt not found, using default message")
        messages = ["Hello from bot!"]

    if not messages:
        messages = ["Hello from bot!"]

    # --- Load proxies (optional) ---
    proxies = []
    try:
        with open('proxies.txt', 'r', encoding='utf-8') as f:
            proxies = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        proxies = []

    # --- Settings ---
    channel = input("Введите название канала (без #): ").strip().lower()
    if not channel:
        logger.error("Канал не указан")
        return

    try:
        delay = int(input("Введите задержку между отправками в секундах (например, 30): ").strip())
        if delay < 1:
            delay = 1
    except Exception:
        delay = 30

    # --- Create bot objects ---
    bots = []
    for i, (username, oauth) in enumerate(accounts):
        proxy = proxies[i] if i < len(proxies) else None
        bot = TwitchBot(username, oauth, proxy=proxy, channel=channel)
        bots.append(bot)

    logger.info(f"Preparing {len(bots)} bots for channel #{channel}")

    # --- Connect all bots (attempts) ---
    for bot in bots:
        try:
            await bot.ensure_connected(reconnect_delay=5, max_attempts=3)
        except Exception:
            logger.warning(f"{bot.username} failed to connect initially")

    # --- Main sending loop: по очереди аккаунт->сообщение->задержка ---
    try:
        while True:
            for i, bot in enumerate(bots):
                if bot._stop:
                    continue
                # Подбираем сообщение по индексу (wrap если нужно)
                msg = messages[i % len(messages)]
                # Убедимся что бот подключён (попытаемся переподключиться при падении)
                if not bot.connected:
                    logger.info(f"{bot.username} not connected, trying to reconnect before send...")
                    await bot.ensure_connected(reconnect_delay=5, max_attempts=3)

                if bot.connected:
                    try:
                        await bot.send_message(msg)
                    except Exception as e:
                        logger.error(f"{bot.username} failed to send: {e}")
                else:
                    logger.error(f"{bot.username} is offline, skipped message")

                # Задержка между отправками от разных аккаунтов
                await asyncio.sleep(delay)
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        # Close all
        close_tasks = [bot.close() for bot in bots]
        await asyncio.gather(*close_tasks, return_exceptions=True)
        logger.info("All bots stopped.")


if __name__ == '__main__':
    asyncio.run(main())