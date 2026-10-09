"""Modbus TCP Slave (从机 / 服务端) 模拟器.

基于 pymodbus 构建，提供线圈(Coil)与保持寄存器(Holding Register)的模拟存储区，
并在后台定时更新传感器模拟数据（如温度、湿度）。
"""

import asyncio
import logging
import random
import sys
import warnings

# 忽略库版本平滑迁移的警告
warnings.filterwarnings("ignore", category=DeprecationWarning)

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ModbusSlave")

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5020
SLAVE_ID = 1


async def update_simulation_data(context: ModbusServerContext) -> None:
    """后台任务：模拟传感器数据变化，每 2 秒更新一次保持寄存器中的数据."""
    logger.info("后台传感器模拟任务已启动...")
    device_context = context[SLAVE_ID]

    temp = 25.0
    humidity = 60

    while True:
        await asyncio.sleep(2)
        # 模拟温度波动 (+/- 0.5 ℃)
        temp = round(temp + random.uniform(-0.5, 0.5), 1)
        humidity = max(30, min(90, humidity + random.randint(-1, 1)))

        # 将浮点数温度转换为定点数存储（乘以 10，例如 25.5 ℃ -> 255）
        temp_val = int(temp * 10)

        # 写入保持寄存器：
        # 地址 0: 温度 (0.1 ℃)
        # 地址 1: 湿度 (%RH)
        # 注：pymodbus 内部存储从 1-based 或 0-based 映射，setValues(3, 0, ...) 代表 3=Holding Register
        device_context.setValues(3, 0, [temp_val, humidity])
        logger.info(
            f"[模拟更新] 当前保持寄存器 => 地址0(温度): {temp:.1f}℃ (值={temp_val}), "
            f"地址1(湿度): {humidity}%RH"
        )


def build_server_context() -> ModbusServerContext:
    """构建 Modbus 数据存储区."""
    # 初始化 100 个寄存器/线圈空间
    # 0x: 线圈 (Coils)
    coils = ModbusSequentialDataBlock(1, [False] * 100)
    # 1x: 离散输入 (Discrete Inputs)
    discrete_inputs = ModbusSequentialDataBlock(1, [False] * 100)
    # 3x: 输入寄存器 (Input Registers)
    input_registers = ModbusSequentialDataBlock(1, [0] * 100)
    # 4x: 保持寄存器 (Holding Registers)
    # 预设：地址 0 为 250(25.0℃), 地址 1 为 60(60%), 地址 2~4 为预置控制参数
    holding_registers = ModbusSequentialDataBlock(1, [250, 60, 100, 200, 300] + [0] * 95)

    slave_context = ModbusDeviceContext(
        di=discrete_inputs,
        co=coils,
        hr=holding_registers,
        ir=input_registers,
    )
    return ModbusServerContext(devices=slave_context, single=True)


async def main() -> None:
    logger.info("=" * 55)
    logger.info(f"启动 Modbus TCP Slave (从机模拟器) - 监听: {SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"从机设备 ID (Slave/Unit ID): {SLAVE_ID}")
    logger.info("=" * 55)

    context = build_server_context()

    # 启动后台数据模拟协程
    sim_task = asyncio.create_task(update_simulation_data(context))

    try:
        # 启动 Modbus TCP 异步服务端
        await StartAsyncTcpServer(
            context=context,
            address=(SERVER_HOST, SERVER_PORT),
        )
    except asyncio.CancelledError:
        logger.info("服务端正在停止...")
    finally:
        sim_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("收到退出信号，服务已平稳关闭。")
        sys.exit(0)
