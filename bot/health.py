from aiohttp import web
from loguru import logger


async def health_handler(request: web.Request) -> web.Response:
    pool = request.app.get('pool')

    if pool is None:
        return web.Response(text='no pool configured', status=503)

    try:
        await pool.fetchval('SELECT 1')
    except Exception as e:
        logger.error(f"HEALTH_CHECK_DB_FAILED | {e}")
        return web.Response(text='db unreachable', status=503)

    return web.Response(text='ok', status=200)


async def start_health_server(port: int = 8080, pool=None) -> web.AppRunner:
    app = web.Application()
    app['pool'] = pool
    app.router.add_get('/health', health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    logger.info(f"Health server started | port={port}")
    return runner
