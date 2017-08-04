FROM python:2.7

RUN pip install luigi elasticsearch requests

WORKDIR /usr/src/app

ADD . /usr/src/app

ENTRYPOINT ["python", "happymap_tweets.py", "ESIndexing", "--filename", "madrid40000.json", "--local-scheduler"]
