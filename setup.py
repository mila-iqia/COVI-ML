from setuptools import setup


with open('requirements.txt', 'r') as f:
    requirements = [line.strip() for line in f.readlines() if not line.startswith("#")]

setup(
    name='ctt',
    version='0.1',
    packages=['ctt', 'ctt.frozen', 'ctt.models', 'ctt.serving', 'ctt.inference', 'ctt.conversion', 'ctt.data_loading'],
    url='https://github.com/nasimrahaman/ctt',
    license='MIT',
    author='Nasim Rahaman',
    author_email='nasim.rahaman@tuebingen.mpg.de',
    description='Contact Tracing Transformer',
    install_requires=requirements
)
