"""NetWatch setup configuration."""

from setuptools import find_packages, setup

setup(
    name="netwatch",
    version="1.0.0",
    description="Lightweight real-time network monitoring utility",
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.9",
    install_requires=[
        "PyYAML>=6.0",
        "tabulate>=0.9.0",
        "jsonschema>=4.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "pytest-cov>=4.0",
            "flake8>=6.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "netwatch=netwatch.__main__:main",
        ],
    },
)
