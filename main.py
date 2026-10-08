import sys


def main() -> None:
    print("=" * 45)
    print("Python 开发环境已成功就绪！")
    print(f"Python 版本: {sys.version.split()[0]}")
    print(f"解释器路径: {sys.executable}")
    print("=" * 45)


if __name__ == "__main__":
    main()
