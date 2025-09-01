# Thunder/__main__.py

import asyncio
import glob
import importlib.util
import sys
from datetime import datetime

from uvloop import install
from pathlib import Path

install()

from aiohttp import web
from pyrogram import idle

from Thunder import __version__
from Thunder.bot import StreamBot
from Thunder.bot.clients import cleanup_clients, initialize_clients
from Thunder.server import web_server
from Thunder.utils.commands import set_commands
from Thunder.utils.database import db
from Thunder.utils.handler import handle_flood_wait
from Thunder.utils.keepalive import ping_server
from Thunder.utils.logger import logger
from Thunder.utils.messages import MSG_ADMIN_RESTART_DONE
from Thunder.bot.plugins.stream import link_handler, private_receive_handler
from Thunder.utils.rate_limiter import rate_limiter
from Thunder.utils.tokens import cleanup_expired_tokens
from Thunder.vars import Var

PLUGIN_PATH = "Thunder/bot/plugins/*.py"
VERSION = __version__

def print_banner():
    banner = f"""
 ____  ____  _____    ____  _____  ____  ____ 
(  _ \(  _ \(  _  )  (  _ \(  _  )(_  _)(_   )
 )___/ )   / )(_)(    ) _ < )(_)(   )(   / /_ 
(__)  (_)\_)(_____)  (____/(_____) (__) (____)
"""
    print(banner)

async def import_plugins():
    print("╠════════════════════ IMPORTING PLUGINS ════════════════════╣")
    plugins = glob.glob(PLUGIN_PATH)
    if not plugins:
        print("   ▶ No plugins found to import!")
        return 0

    success_count = 0
    failed_plugins = []

    for file_path in plugins:
        try:
            plugin_path = Path(file_path)
            plugin_name = plugin_path.stem
            import_path = f"Thunder.bot.plugins.{plugin_name}"

            spec = importlib.util.spec_from_file_location(import_path, plugin_path)
            if spec is None or spec.loader is None:
                logger.error(f"Invalid plugin specification for {plugin_name}")
                failed_plugins.append(plugin_name)
                continue

            module = importlib.util.module_from_spec(spec)
            sys.modules[import_path] = module
            spec.loader.exec_module(module)
            success_count += 1

        except Exception as e:
            plugin_name = Path(file_path).stem
            logger.error(f"   ✖ Failed to import plugin {plugin_name}: {e}")
            failed_plugins.append(plugin_name)

    print(f"   ▶ Total: {len(plugins)} | Success: {success_count} | Failed: {len(failed_plugins)}")
    if failed_plugins:
        print(f"   ▶ Failed plugins: {', '.join(failed_plugins)}")

    return success_count

async def start_services():
    start_time = datetime.now()
    print_banner()
    print("╔════════════════ INITIALIZING BOT SERVICES ════════════════╗")

    print("   ▶ Starting Telegram Bot initialization...")
    try:
        await handle_flood_wait(StreamBot.start)
        bot_info = await handle_flood_wait(StreamBot.get_me)
        StreamBot.username = bot_info.username
        print(f"   ✓ Bot initialized successfully as @{StreamBot.username}")

        await set_commands()
        print("   ✓ Bot commands set successfully.")

        restart_message_data = await db.get_restart_message()
        if restart_message_data:
            try:
                await handle_flood_wait(
                    StreamBot.edit_message_text,
                    chat_id=restart_message_data["chat_id"],
                    message_id=restart_message_data["message_id"],
                    text=MSG_ADMIN_RESTART_DONE
                )
                await db.delete_restart_message(restart_message_data["message_id"])
            except Exception as e:
                logger.error(f"Error processing restart message: {e}", exc_info=True)
        else:
            pass

    except Exception as e:
        logger.error(f"   ✖ Failed to initialize Telegram Bot: {e}", exc_info=True)
        return

    print("   ▶ Starting Client initialization...")
    try:
        await initialize_clients()
    except Exception as e:
        logger.error(f"   ✖ Failed to initialize clients: {e}", exc_info=True)
        return

    await import_plugins()

    print("   ▶ Starting Rate Limiter and Executor initialization...")
    try:
        request_executor_task = asyncio.create_task(request_executor())
        print("   ✓ Request executor service started")
    except Exception as e:
        logger.error(f"   ✖ Failed to start request executor: {e}", exc_info=True)
        return

    print("   ▶ Starting Web Server initialization...")
    try:
        app_runner = web.AppRunner(await web_server())
        await app_runner.setup()
        bind_address = Var.BIND_ADDRESS
        site = web.TCPSite(app_runner, bind_address, Var.PORT)
        await site.start()

        keepalive_task = asyncio.create_task(ping_server())
        print("   ✓ Keep-alive service started")
        token_cleanup_task = asyncio.create_task(schedule_token_cleanup())

    except Exception as e:
        logger.error(f"   ✖ Failed to start Web Server: {e}", exc_info=True)
        return

    elapsed_time = (datetime.now() - start_time).total_seconds()
    print("╠═══════════════════════════════════════════════════════════╣")
    print(f"   ▶ Bot Name: {bot_info.first_name}")
    print(f"   ▶ Username: @{bot_info.username}")
    print(f"   ▶ Server: {bind_address}:{Var.PORT}")
    print(f"   ▶ Owner: {Var.OWNER_USERNAME}")
    print(f"   ▶ Startup Time: {elapsed_time:.2f} seconds")
    print("╚═══════════════════════════════════════════════════════════╝")
    print("   ▶ Bot is now running! Press CTRL+C to stop.")

    try:
        await idle()
    finally:
        print("   ▶ Shutting down services...")
        
        for task in [locals().get("keepalive_task"), locals().get("token_cleanup_task"), locals().get("request_executor_task")]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        try:
            await rate_limiter.shutdown()
        except Exception as e:
            logger.error(f"Error during rate limiter cleanup: {e}")

        try:
            await cleanup_clients()
        except Exception as e:
            logger.error(f"Error during client cleanup: {e}")

        if 'app_runner' in locals() and app_runner is not None:
            try:
                await app_runner.cleanup()
            except Exception as e:
                logger.error(f"Error during web server cleanup: {e}")

async def schedule_token_cleanup():
    while True:
        try:
            await asyncio.sleep(3 * 3600)
            await cleanup_expired_tokens()
        except asyncio.CancelledError:
            logger.debug("schedule_token_cleanup cancelled cleanly.")
            break
        except Exception as e:
            logger.error(f"Token cleanup error: {e}", exc_info=True)

async def request_executor():
    logger.info("Request executor started")
    async for request_data in rate_limiter.queue_consumer():
        try:
            handler_type = request_data.get('handler_type')
            bot = request_data.get('bot')
            message = request_data.get('message')
            kwargs = request_data.get('kwargs', {})
            user_id = request_data.get('user_id')

            if not all([handler_type, bot, message, user_id]):
                logger.error(f"Skipping invalid request data: {request_data}")
                continue

            logger.debug(f"Executing queued {handler_type} request for user {user_id}")

            if handler_type == 'private':
                await private_receive_handler(bot, message, **kwargs)
            elif handler_type == 'link':
                await link_handler(bot, message, **kwargs)
            else:
                logger.error(f"Unknown handler type in request executor: {handler_type}")

        except asyncio.CancelledError:
            logger.info("Request executor cancelled, shutting down.")
            break
        except Exception as e:
            logger.error(f"Error executing request for user {request_data.get('user_id')}: {e}", exc_info=True)

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        print("╔═══════════════════════════════════════════════════════════╗")
        print("║                   Bot stopped by user (CTRL+C)            ║")
        print("╚═══════════════════════════════════════════════════════════╝")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        loop.close()

