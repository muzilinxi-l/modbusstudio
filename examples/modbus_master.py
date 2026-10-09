"""Modbus TCP Master (主机 / 客户端).

基于 pymodbus 构建，主动向 Modbus Slave (从机) 发起连接与读写操作：
- 读取保持寄存器 (FC 03)
- 写入单个保持寄存器 (FC 06) / 写入多个保持寄存器 (FC 16)
- 控制与读取开关线圈 (FC 01 / FC 05)
"""

import asyncio
import logging
import sys

from pymodbus.client import AsyncModbusTcpClient

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ModbusMaster")

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5020
SLAVE_ID = 1


async def main() -> None:
    logger.info("=" * 55)
    logger.info(f"Modbus TCP Master 正在连接从机: {SERVER_HOST}:{SERVER_PORT}")
    logger.info("=" * 55)

    client = AsyncModbusTcpClient(SERVER_HOST, port=SERVER_PORT)
    connected = await client.connect()

    if not connected:
        logger.error(f"连接从机失败，请确保 modbus_slave.py 已经在运行！")
        return

    logger.info("已成功连接到 Modbus 从机！开始执行测试交互...\n")

    try:
        # ---------------------------------------------------------
        # 1. 读取保持寄存器 (Holding Registers - 功能码 03)
        # ---------------------------------------------------------
        logger.info("[测试 1] 读取保持寄存器 (起始地址=0, 读取数量=5)...")
        rr = await client.read_holding_registers(0, count=5, device_id=SLAVE_ID)
        if rr.isError():
            logger.error(f"读取保持寄存器失败: {rr}")
        else:
            raw_temp = rr.registers[0]
            raw_hum = rr.registers[1]
            temp_c = raw_temp / 10.0
            logger.info(f"-> 原始寄存器值: {rr.registers}")
            logger.info(f"-> 解析传感器数据: 温度={temp_c} ℃, 湿度={raw_hum} %RH\n")

        # ---------------------------------------------------------
        # 2. 写入单个保持寄存器 (Write Single Register - 功能码 06)
        # ---------------------------------------------------------
        test_address = 2
        test_val = 666
        logger.info(f"[测试 2] 向地址 {test_address} 写入参数值: {test_val}...")
        wr = await client.write_register(test_address, test_val, device_id=SLAVE_ID)
        if wr.isError():
            logger.error(f"写入保持寄存器失败: {wr}")
        else:
            # 重新读取验证
            verify_rr = await client.read_holding_registers(test_address, count=1, device_id=SLAVE_ID)
            logger.info(f"-> 写入成功！重新读取地址 {test_address} 当前值为: {verify_rr.registers[0]}\n")

        # ---------------------------------------------------------
        # 3. 读写线圈 (Coils - 模拟开/关继电器 - 功能码 01 & 05)
        # ---------------------------------------------------------
        coil_addr = 0
        logger.info(f"[测试 3] 控制继电器/开关线圈 (地址 {coil_addr} 置为 True)...")
        await client.write_coil(coil_addr, True, device_id=SLAVE_ID)

        rc = await client.read_coils(coil_addr, count=1, device_id=SLAVE_ID)
        if rc.isError():
            logger.error(f"读取线圈状态失败: {rc}")
        else:
            logger.info(f"-> 读取线圈状态: {rc.bits[0]} (True=开 / 通电)\n")

        # ---------------------------------------------------------
        # 4. 连续监控循环 (演示连续采集 3 次从机数据)
        # ---------------------------------------------------------
        logger.info("[测试 4] 连续采集从机数据演示 (共读取 3 次，间隔 2 秒)...")
        for i in range(1, 4):
            await asyncio.sleep(2)
            rr = await client.read_holding_registers(0, count=2, device_id=SLAVE_ID)
            if not rr.isError():
                t = rr.registers[0] / 10.0
                h = rr.registers[1]
                logger.info(f"  [采样 {i}/3] 最新温湿度 => {t:.1f} ℃ / {h} %RH")

        logger.info("\n所有测试操作执行完毕！")

    except Exception as e:
        logger.exception(f"通信过程中出现异常: {e}")
    finally:
        logger.info("关闭 Modbus 客户端连接。")
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
