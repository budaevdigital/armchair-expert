from markov_schema import *
from config import *
from sqlalchemy import and_, or_
from sqlalchemy import func, update, delete
import re
import random
import time
import numpy as np


class MarkovAI(object):
    ALPHANUMERIC = "abcdefghijklmnopqrstuvqxyz123456789"

    def __init__(self):
        self.rebuilding = False
        self.rebuilding_thread = None

    def rebuild_db(self, ignore=[]):

        if self.rebuilding:
            return

        print("Rebuilding DB...")

        self.rebuilding = True
        session = Session()
        session.execute("VACUUM")
        session.query(URL).delete()
        session.query(WordRelation).delete()
        session.query(Word).delete()
        session.commit()

        lines = session.query(Line).order_by(Line.timestamp.asc()).all()
        for line in lines:
            if str(line.channel) in ignore:
                print("!!!NSFW FILTER!!!")
                continue
            elif line.server_id == 0:
                continue

            text = re.sub(r'<@[!]?[0-9]+>', '#nick', line.text)
            print(text)

            self.process_msg(None, text, rebuild_db=True, timestamp=line.timestamp)

        self.rebuilding = False

        session.execute("VACUUM")
        print("Rebuilding DB Complete!")

    @staticmethod
    def clean_db():

        print("Cleaning DB...")
        session = Session()

        # Subtract Rating by 1
        session.execute(update(WordRelation, values={
            WordRelation.rating: WordRelation.rating - CONFIG_MARKOV_TICK_RATING_DAILY_REDUCE}))
        session.commit()

        # Remove all forwards associations with no score
        session.query(WordRelation).filter(WordRelation.rating <= 0).delete()
        session.commit()

        # Check if we have any forward associations left
        results = session.query(Word.id). \
            outerjoin(WordRelation, WordRelation.a == Word.id). \
            group_by(Word.id). \
            having(func.count(WordRelation.id) == 0).all()

        # Go through each word with no forward associations left
        for result in results:
            # First delete all associations backwards from this word to other words
            session.query(WordRelation).filter(WordRelation.b == result.id).delete()
            # Next delete the word
            session.query(Word).filter(Word.id == result.id).delete()

        session.commit()
        session.close()

        print("Cleaning DB Complete!")

    def filter(self, txt):

        s = txt

        # Strip all URL
        s = re.sub('http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',
                   '', s, flags=re.MULTILINE)

        # Convert everything to lowercase
        s = txt.lower()

        s = re.sub(r',|"|;|\(|\)|\[|\]|{|}|%|@|$|\^|&|\*|_|\\|/', "", s)

        sentences = []
        # Split by lines
        for line in s.split("\n"):
            # Split by sentence
            for sentence in re.split(r'\.|!|\?', line):
                # Split by words
                pre_words = sentence.split(" ")
                post_words = []

                for word in pre_words:
                    if word != '':
                        post_words.append(word)

                if len(post_words) >= 1:
                    sentences.append(post_words)

        return sentences

    def learn(self, words):

        session = Session()

        last_b_added = None

        word_index = 0
        for word in words:
            # Add word if it doesn't exist
            word_a = session.query(Word).filter(Word.text == word).first()
            if word_a is None:
                word_a = Word(text=word)
                session.add(word_a)
                session.commit()
            elif last_b_added is None or word != last_b_added.text:
                word_a.count += 1

            # Not last word? Lookup / add association
            if word_index != len(words) - 1:

                # Word B
                word_b = session.query(Word).filter(Word.text == words[word_index + 1]).first()
                if word_b is None:
                    word_b = Word(text=words[word_index + 1])
                    session.add(word_b)
                    session.commit()
                    last_b_added = word_b

                # Add Association
                relation = session.query(WordRelation).filter(
                    and_(WordRelation.a == word_a.id, WordRelation.b == word_b.id)).first()
                if relation is None:
                    session.add(WordRelation(a=word_a.id, b=word_b.id))
                else:
                    relation.count += 1
                    relation.rating += 1

            word_index += 1

        session.commit()

    def cmd_stats(self):
        session = Session()
        words = session.query(Word.id).count()
        lines = session.query(Line.id).count()
        assoc = session.query(WordRelation).count()
        return "I know %d words (%d contexts, %8.2f per word), %d lines." % (
            words, assoc, float(assoc) / float(words), lines)

    def command(self, txt, args=None, is_owner=False):

        result = None

        if txt.startswith("!words"):
            result = self.cmd_stats()

        if is_owner is False:
            return result

        # Admin Only Commands
        if txt.startswith("!clean"):
            self.clean_db()

        return result

    def reply(self, words, args):
        session = Session()

        w = []

        # Find a topic word to base the sentence on. Will be over 4 chars if we have two or more words.
        if len(words) >= 3:
            w = [word for word in words if len(word) >= CONFIG_MARKOV_TOPIC_WORD_MIN_LENGTH]
            w = [word for word in w if word not in CONFIG_MARKOV_TOPIC_FILTER]
        # Otherwise find the longest word that makes it through the filter
        else:
            longest_word = ''

            for word in words:
                if word not in CONFIG_MARKOV_TOPIC_FILTER and len(word) > len(longest_word):
                    longest_word = word

            if longest_word != '':
                w = [longest_word]

        # If we couldn't find any words, use 'nick' instead
        if len(w) == 0:
            w = ['#nick']

        the_word = session.query(Word.id, Word.text, func.count(WordRelation.id).label('relations')). \
            join(WordRelation, WordRelation.a == Word.id). \
            filter(Word.text.in_(w)). \
            group_by(Word.id, Word.text). \
            order_by(func.count(WordRelation.id).desc()).first()

        if the_word is None:
            return None

        # Generate Backwards
        backwards_words = []
        f_id = the_word.id
        back_count = random.randrange(0, CONFIG_MARKOV_VECTOR_LENGTH)
        count = 0
        while count < back_count:

            results = session.query(WordRelation.a, Word.text). \
                join(Word, WordRelation.a == Word.id). \
                order_by(WordRelation.rating). \
                filter(and_(WordRelation.b == f_id,WordRelation.a != f_id)).all()

            if len(results) == 0:
                break

            #Pick a random result
            r_index = int(np.random.beta(0.5, 0.5) * len(results))

            r = results[r_index]

            f_id = r.a
            backwards_words.insert(0, r.text)

            count += 1

        # Generate Forwards
        forward_words = []
        f_id = the_word.id
        forward_count = random.randrange(0, CONFIG_MARKOV_VECTOR_LENGTH)
        count = 0
        while count < forward_count:

            results = session.query(WordRelation.b, Word.text). \
                join(Word, WordRelation.b == Word.id). \
                order_by(WordRelation.rating). \
                filter(and_(WordRelation.a == f_id,WordRelation.b != f_id)).all()

            if len(results) == 0:
                break

            # Pick a random result
            r_index = int(np.random.beta(0.5,0.5) * len(results))

            r = results[r_index]

            f_id = r.b
            forward_words.append(r.text)

            count += 1

        reply = []

        reply += backwards_words
        reply += [the_word.text]
        reply += forward_words

        # Replace any mention in response with a mention to the name of the message we are responding too
        reply = [word.replace('#nick', args['author_mention']) for word in reply]

        # Add a random URL
        if random.randrange(0, 100) > (100 - CONFIG_MARKOV_URL_CHANCE):
            url = session.query(URL).order_by(func.random()).first()
            if url is not None:
                reply.append(url.text)

        return " ".join(reply)

    def process_msg(self, io_module, txt, replyrate=1, args=None, owner=False, rebuild_db=False, timestamp=None, learning=True):

        # Ignore external I/O while rebuilding
        if self.rebuilding is True and not rebuild_db:
            return

        if txt.strip() == '':
            return

        if not rebuild_db:
            session = Session()
            session.add(Line(text=txt, author=args['author'], server_id=args['server'], channel=str(args['channel'])))
            session.commit()

            # Check for command
            if txt.startswith("!"):
                result = self.command(txt, args, owner)
                if result:
                    io_module.output(result, args)
                return

        if learning:
            # Get all URLs
            urls = re.findall('http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', txt)
            if len(urls) >= 0:
                session = Session()
                for url in urls:

                    the_url = session.query(URL).filter(URL.text == url).first()

                    if the_url is not None:
                        the_url.count += 1
                    else:
                        if timestamp:
                            session.add(URL(text=url, timestamp=timestamp))
                        else:
                            session.add(URL(text=url))

                session.commit()

        sentences = self.filter(txt)
        if len(sentences) == 0:
            return

        sentence_index = 0
        reply_sentence = random.randrange(0, len(sentences))

        for sentence in sentences:
            if learning:
                self.learn(sentence)

            if not rebuild_db:
                if reply_sentence == sentence_index and replyrate > random.randrange(0, 100):
                    io_module.output(self.reply(sentence, args), args)

            sentence_index += 1
