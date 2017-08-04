import luigi
import json
import requests
import datetime
from time import sleep
from elasticsearch import ConnectionError
from luigi.contrib.esindex import CopyToIndex

SENT_ANALYSIS_ENDPOINT = "http://test.senpy.cluster.gsi.dit.upm.es/api/?algo={algorithm}&i={text}&language={lang}"
SENPY_LANGUAGE_EN = 'en'
SENPY_LANGUAGE_ES = 'es'
SENPY_DEFAULT_ALGORITHM = 'emotion-anew'
SENPY_AROUSAL_ENDPOINT = 'http://www.gsi.dit.upm.es/ontologies/onyx/vocabularies/anew/ns#arousal'
SENPY_DOMINANCE_ENDPOINT = 'http://www.gsi.dit.upm.es/ontologies/onyx/vocabularies/anew/ns#dominance'
SENPY_VALENCE_ENDPOINT = 'http://www.gsi.dit.upm.es/ontologies/onyx/vocabularies/anew/ns#valence'
SENPY_EMOTION_KEY = 'onyx:hasEmotionCategory'

# -------------------------------------------- #
#            ElasticSearch params
# -------------------------------------------- #
TWEETS_INDEX = 'tweets_happymap'
ELASTIC_DOC_TYPE = 'tweet'
ATTR_ID = 'id'
ATTR_TEXT = 'text'
ATTR_SOURCE = '_source'
ATTR_TIMESTAMP = 'timestamp_ms'
ATTR_HITS = 'hits'
ATTR_SENTIMENT_ANALYSIS = 'sentiment_analysis'
ATTR_COORDINATES = 'coordinates'
ATTR_BOUNDING_BOX = 'bounding_box'
ATTR_PLACE = 'place'
TEN_HOURS_MS = 36000000


# -------------------------------------------- #
#                  Messages
# -------------------------------------------- #
LOG_TWEET_SAVED_FILE = 'Tweet saved in file: {tweet}'
LOG_TWEET_GOT_FROM_ES = 'Tweet got from ElasticSearch: {tweet}'
LOG_TWEET_PUT_INTO_ES = 'PUT: {tweet}'
LOG_TWEET_DELETED_FROM_ES = 'DELETE: {tweet}'
LOG_TWEET_NOT_GEO = 'Tweet {tweet_id} has not geo info'


class Preprocessing(luigi.Task):

    filename = luigi.Parameter()  # Luigi parameter which points to the output file. It must be under /data
    cache_latlng = list()

    def run(self):
        """
        Actual task execution
        """
        count = 0
        next_timestamp = 0
        with self.output().open('w') as output:
            with open(self.filename, 'r') as infile:
                for i, line in enumerate(infile):
                    if count == 10000:
                        break
                    try:
                        tweet = json.loads(line)
                    except Exception:
                        print('Preprocessing - N{number}: EXCEPTION.'.format(number=i))
                        continue
                    if not ensure_is_geolocated(tweet=tweet) or \
                            tweet[ATTR_COORDINATES][ATTR_COORDINATES] in self.cache_latlng:
                        print('Preprocessing - N{number}: FAIL: Not GEO info.'.format(number=i))
                        continue
                    if next_timestamp == 0:
                        next_timestamp = int(tweet[ATTR_TIMESTAMP])
                    tweet[ATTR_TIMESTAMP] = next_timestamp
                    output.write(json.dumps(tweet))
                    print('Preprocessing - N{number}: SUCCESS.'.format(number=i))
                    output.write('\n')
                    self.cache_latlng.append(tweet[ATTR_COORDINATES][ATTR_COORDINATES])
                    count += 1
                    next_timestamp = int(next_timestamp) - TEN_HOURS_MS

    def output(self):
        return luigi.LocalTarget(path='preprocessed-%s' % self.filename)


class SenpyTweets(luigi.Task):

    filename = luigi.Parameter()

    def requires(self):
        return Preprocessing(self.filename)

    def run(self):
        """
        Actual task execution
        """
        with self.output().open('w') as output:
            with self.input().open('r') as infile:
                for i, line in enumerate(infile):
                    tweet = json.loads(line)
                    if not ensure_is_geolocated(tweet=tweet):  # Ensure the tweet has geo info. If not, next.
                        print(LOG_TWEET_NOT_GEO.format(tweet_id=tweet[ATTR_ID]))
                        continue
                    if tweet.get(ATTR_SENTIMENT_ANALYSIS):
                        continue
                    tweet[ATTR_SENTIMENT_ANALYSIS] = sentiment_analysis(tweet.get(ATTR_TEXT))
                    if not tweet[ATTR_SENTIMENT_ANALYSIS]:
                        print('SenpyTweets - N{number}: FAIL: Tweet was neutral.'.format(number=i))
                        continue
                    tweet[ATTR_TIMESTAMP] = int(float(tweet[ATTR_TIMESTAMP]))
                    output.write(json.dumps(tweet))
                    print('SenpyTweets - N{number}: SUCCESS'.format(number=i))
                    output.write('\n')

    def output(self):
        return luigi.LocalTarget(path='senpy-%s' % self.filename)


class ESIndexing(CopyToIndex):
    filename = luigi.Parameter()
    date = luigi.DateParameter(default=datetime.date.today())
    index = TWEETS_INDEX
    doc_type = ELASTIC_DOC_TYPE
    host = 'elasticsearch'
    port = 9200

    def requires(self):
        # print('Waiting 10 secs for elasticsearch')
        # sleep(10)
        return SenpyTweets(self.filename)

    def create_index(self):
        """
        Override to provide code for creating the target index.

        By default it will be created without any special settings or mappings.
        """
        es = self._init_connection()
        connected = False
        while not connected:
            try:
                if not es.indices.exists(index=self.index):
                    es.indices.create(index=self.index, body=self.settings)
                connected = True
            except ConnectionError as exc:
                print('Waiting for Elasticsearch... Sleep(3)')
                sleep(3)

def ensure_is_geolocated(tweet):
    """
    This function gets a tweet and ensures that, if it has relevant geo info, it is displayed in the "coordinates"
    attribute. If there is not "coordinates" attribute, but it is "places" points (see Twitter API for object models),
    "coordinates" attribute will be the average of that points.
    :param tweet: (dict) input tweet
    :return: (bool) whether or no the tweet has geo info
    """
    try:
        # Case tweet is correctly located
        if tweet.get(ATTR_COORDINATES):
            return tweet
        # Case tweet is not correctly located, but there is "places" info
        elif tweet.get(ATTR_PLACE):
            polygon_points = tweet[ATTR_PLACE][ATTR_BOUNDING_BOX][ATTR_COORDINATES][0]
            total_lon = total_lat = 0
            for point in polygon_points:
                total_lon += point[0]
                total_lat += point[1]
            avg_lon = total_lon/len(polygon_points)
            avg_lat = total_lat/len(polygon_points)
            tweet[ATTR_COORDINATES] = {}
            tweet[ATTR_COORDINATES]['type'] = 'Point'
            tweet[ATTR_COORDINATES][ATTR_COORDINATES] = [avg_lon, avg_lat]
            return tweet
        # Case tweet is not located
        else:
            return False
    except Exception as exc:
        print(exc)
        return False


def sentiment_analysis(text):
    """
    This function gets a text and return a full sentiment analysis.
    :param text: (str) text to analyze
    :return: (dict) sentiment analysis
    """
    try:
        uri = SENT_ANALYSIS_ENDPOINT.format(
            algorithm=SENPY_DEFAULT_ALGORITHM,
            lang=SENPY_LANGUAGE_ES,
            text=text.encode('utf-8')
        )
        get_res = requests.request("GET", uri, headers={'content-type': 'application/x-www-form-urlencoded'})\
            .content.decode('utf-8')
        res_dict = json.loads(get_res).get('entries')[0].get('emotions')[0].get('onyx:hasEmotion')
        sentiment_data = {
            'valence': res_dict.get(SENPY_VALENCE_ENDPOINT)/10,
            'arousal': res_dict.get(SENPY_AROUSAL_ENDPOINT)/10,
            'dominance': res_dict.get(SENPY_DOMINANCE_ENDPOINT)/10,
            'emotion': res_dict.get(SENPY_EMOTION_KEY).split('#')[1]
        }
        if(sentiment_data.get('valence') == 0 and sentiment_data.get('arousal') == 0 and
           sentiment_data.get('dominance') == 0):
            return 0
        else:
            return sentiment_data
    except Exception as exc:
        raise Exception("ERROR EN REQUESTS. {0}".format(exc))

if __name__ == '__main__':
    luigi.run()
