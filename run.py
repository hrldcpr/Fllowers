from gevent import monkey; monkey.patch_all()

import datetime
import logging
import random
import time

import gevent
import requests

import api
import database


MAX_FOLLOWS_PER_DAY = 400
MAX_LEADER_RATIO = 1.5
EXTRA_LEADERS = 500

FOLLOW_PERIOD = datetime.timedelta(seconds=5)
DAY = datetime.timedelta(days=1)
USER_FOLLOWERS_UPDATE_PERIOD = 0.25 * DAY
UPDATE_PERIOD = 3 * DAY
UNFOLLOW_LEADERS_PERIOD = 2 * DAY
UNFOLLOW_FOLLOWERS_PERIOD = 28 * DAY


def now():
    return datetime.datetime.now(datetime.timezone.utc)

def log(user, message, *args, level=logging.INFO):
    logging.log(level, '[%s] ' + message, user.screen_name, *args)

def warn(user, message, *args):
    log(user, message, *args, level=logging.WARNING)


def get_keeper_ids(db, user, retry=True):
    try:
        data = api.get(user, 'lists/members', slug='fllow-keepers',
                       owner_screen_name=user.screen_name, count=5000, skip_status=True)
    except requests.exceptions.HTTPError as e:
        warn(user, 'fllow-keepers list not found')
        if e.response.status_code == 404 and retry:
            api.post(user, 'lists/create', name='fllow keepers', mode='private',
                     description='fllow will not unfollow users in this list')
            return get_keeper_ids(db, user, retry=False)
        raise e

    with db, db.cursor() as cursor:
        return database.add_twitter_api_ids(cursor, [user['id'] for user in data['users']])


def update_outsiders(db, user, outsider_ids, retry=True):
    try:
        data = api.get(user, 'lists/members', slug='fllow-outsiders',
                       owner_screen_name=user.screen_name, count=5000, skip_status=True)
    except requests.exceptions.HTTPError as e:
        warn(user, 'fllow-outsiders list not found')
        if e.response.status_code == 404 and retry:
            api.post(user, 'lists/create', name='fllow outsiders', mode='private',
                     description="users you manually followed / fllow didn't automatically follow")
            return update_outsiders(db, user, outsider_ids, retry=False)
        raise e

    current_api_ids = {user['id'] for user in data['users']}
    with db, db.cursor() as cursor:
        api_ids = database.get_twitter_api_ids(cursor, outsider_ids)

    added_api_ids = list(api_ids - current_api_ids)
    log(user, 'adding %d outsiders', len(added_api_ids))
    for i in range(0, len(added_api_ids), 100):
        api.post(user, 'lists/members/create_all', slug='fllow-outsiders',
                 owner_screen_name=user.screen_name,
                 user_id=','.join(str(api_id) for api_id in added_api_ids[i:i+100]))

    removed_api_ids = list(current_api_ids - api_ids)
    log(user, 'removing %d outsiders', len(removed_api_ids))
    for i in range(0, len(removed_api_ids), 100):
        api.post(user, 'lists/members/destroy_all', slug='fllow-outsiders',
                 owner_screen_name=user.screen_name,
                 user_id=','.join(str(api_id) for api_id in removed_api_ids[i:i+100]))


def update_leaders(db, user, follower_id):
    # only update leaders if they haven't been updated recently:
    with db, db.cursor() as cursor:
        twitter = database.get_twitter(cursor, follower_id)
    log(user, 'maybe updating leaders for %s updated at %s',
        twitter.screen_name, twitter.leaders_updated_time)
    if twitter.leaders_updated_time and twitter.leaders_updated_time > now() - UPDATE_PERIOD:
        return log(user, 'updated too recently')

    start_time = now()
    api_cursor = -1  # cursor=-1 requests first page
    while api_cursor:  # cursor=0 means no more pages
        log(user, 'getting cursor=%s', api_cursor)
        data = api.get(user, 'friends/ids', user_id=twitter.api_id, cursor=api_cursor)
        api_cursor = data['next_cursor']
        log(user, 'got %d leaders, next_cursor=%s', len(data['ids']), api_cursor)

        with db, db.cursor() as cursor:
            leader_ids = database.add_twitter_api_ids(cursor, data['ids'])
            database.update_twitter_leaders(cursor, follower_id, leader_ids)

    # delete leaders who weren't seen again:
    with db, db.cursor() as cursor:
        database.delete_old_twitter_leaders(cursor, follower_id, start_time)
        database.update_twitter_leaders_updated_time(cursor, follower_id, start_time)
    return True


def update_followers(db, user, leader_id, update_period=UPDATE_PERIOD):
    # only update followers if they haven't been updated recently:
    with db, db.cursor() as cursor:
        twitter = database.get_twitter(cursor, leader_id)
    log(user, 'maybe updating followers for %s updated at %s',
        twitter.screen_name, twitter.followers_updated_time)
    if twitter.followers_updated_time and twitter.followers_updated_time > now() - update_period:
        return log(user, 'updated too recently')

    start_time = now()
    api_cursor = -1  # cursor=-1 requests first page
    while api_cursor:  # cursor=0 means no more pages
        log(user, 'getting cursor=%s', api_cursor)
        data = api.get(user, 'followers/ids', user_id=twitter.api_id, cursor=api_cursor)
        api_cursor = data['next_cursor']
        log(user, 'got %d followers, next_cursor=%s', len(data['ids']), api_cursor)

        with db, db.cursor() as cursor:
            follower_ids = database.add_twitter_api_ids(cursor, data['ids'])
            database.update_twitter_followers(cursor, leader_id, follower_ids)

    # delete followers who weren't seen again:
    with db, db.cursor() as cursor:
        database.delete_old_twitter_followers(cursor, leader_id, start_time)
        database.update_twitter_followers_updated_time(cursor, leader_id, start_time)


def unfollow(db, user, leader_id):
    # only unfollow someone if this user followed them,
    # and they've had some time to follow them back but didn't:
    with db, db.cursor() as cursor:
        twitter = database.get_twitter(cursor, leader_id)
        user_follow = database.get_user_follow(cursor, user.id, leader_id)
        user_unfollow = database.get_user_unfollow(cursor, user.id, leader_id)
        follower = database.get_twitter_follower(cursor, user.twitter_id, leader_id)
    log(user, 'unfollowing %s followed at %s',
        twitter.api_id, user_follow.time if user_follow else None)
    if not user_follow:
        return warn(user, 'but they were never followed')
    if user_unfollow:
        return warn(user, 'but they were already unfollowed at %s', user_unfollow.time)
    if user_follow.time > now() - UNFOLLOW_LEADERS_PERIOD:
        return warn(user, 'but they were followed too recently')
    if follower and user_follow.time > now() - UNFOLLOW_FOLLOWERS_PERIOD:
        return warn(user, 'but they were followed too recently for someone who followed back')

    try:
        api.post(user, 'friendships/destroy', user_id=twitter.api_id)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code != 404:
            raise e
        return warn(user, 'failed to unfollow %s [%d %s]',
                    twitter.api_id, e.response.status_code, e.response.text)

    with db, db.cursor() as cursor:
        database.add_user_unfollow(cursor, user.id, leader_id)
        database.delete_twitter_follower(cursor, leader_id, user.twitter_id)


def follow(db, user, leader_id):
    # only follow someone if this user hasn't already followed them,
    # and hasn't followed anyone too recently,
    # and hasn't followed too many people recently:
    with db, db.cursor() as cursor:
        twitter = database.get_twitter(cursor, leader_id)
        user_follow = database.get_user_follow(cursor, user.id, leader_id)
        last_follow_time = database.get_user_follows_last_time(cursor, user.id)
        follows_today = database.get_user_follows_count(cursor, user.id, now() - DAY)
    log(user, 'following %s last followed at %s and %d follows today',
        twitter.api_id, last_follow_time, follows_today)
    if user_follow:
        return warn(user, 'but they were already followed at %s', user_follow.time)
    if last_follow_time and last_follow_time > now() - FOLLOW_PERIOD:
        return warn(user, 'but followed too recently')
    if follows_today >= MAX_FOLLOWS_PER_DAY:
        return warn(user, 'but too many follows today')

    try:
        api.post(user, 'friendships/create', user_id=twitter.api_id)
        followed = True
    except requests.exceptions.HTTPError as e:
        if e.response.status_code != 403:
            raise e
        # 403 can mean blocked or already following, so we mark as followed
        warn(user, 'marking %s as followed [%d %s]',
             twitter.api_id, e.response.status_code, e.response.text)
        followed = False

    with db, db.cursor() as cursor:
        database.add_user_follow(cursor, user.id, leader_id)
        if followed:
            database.update_twitter_followers(cursor, leader_id, [user.twitter_id])


def run(db, user):
    keeper_ids = get_keeper_ids(db, user)
    log(user, '%d keepers', len(keeper_ids))

    with db, db.cursor() as cursor:
        mentors = database.get_user_mentors(cursor, user.id)
    for mentor in mentors:
        try:
            update_followers(db, user, mentor.id)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code != 404:
                raise e
            warn(user, 'mentor %s no longer exists', mentor.screen_name)

    did_update_leaders = update_leaders(db, user, user.twitter_id)
    update_followers(db, user, user.twitter_id, update_period=USER_FOLLOWERS_UPDATE_PERIOD)

    with db, db.cursor() as cursor:
        leader_ids = database.get_twitter_leader_ids(cursor, user.twitter_id)
        follower_ids = database.get_twitter_follower_ids(cursor, user.twitter_id)
        unfollowed_ids = database.get_user_unfollow_leader_ids(cursor, user.id)
        follows = database.get_user_follows(cursor, user.id)
    followed_ids = {f.leader_id for f in follows}
    insider_ids = followed_ids - unfollowed_ids
    outsider_ids = leader_ids - insider_ids
    desaparecidos = insider_ids - leader_ids

    log(user, '%d desaparecidos, for example: %s', len(desaparecidos), list(desaparecidos)[:3])
    log(user, '%d followers', len(follower_ids))
    log(user, '%d currently followed', len(leader_ids))
    log(user, '…of whom %d are outsiders', len(outsider_ids))

    if did_update_leaders:
        update_outsiders(db, user, outsider_ids)

    log(user, '%d unfollowed', len(unfollowed_ids))
    log(user, '%d followed back', len(followed_ids & follower_ids))
    log(user, '%d followed', len(follows))

    unfollow_followers_before = now() - UNFOLLOW_FOLLOWERS_PERIOD
    unfollow_follower_ids = {f.leader_id for f in follows
                             if f.time < unfollow_followers_before}
    log(user, '…of whom %d were followed before %s',
        len(unfollow_follower_ids), unfollow_followers_before)

    unfollow_leaders_before = now() - UNFOLLOW_LEADERS_PERIOD
    unfollow_leader_ids = {f.leader_id for f in follows
                           if f.time < unfollow_leaders_before} - follower_ids
    log(user, '…and %d were followed before %s and have not followed back',
        len(unfollow_leader_ids - unfollow_follower_ids), unfollow_leaders_before)

    unfollow_ids = unfollow_follower_ids | unfollow_leader_ids
    log(user, '…for a total of %d follows', len(unfollow_ids))

    unfollow_ids &= leader_ids  # don't unfollow people we aren't following
    log(user, '…of whom %d are still followed', len(unfollow_ids))

    unfollow_ids -= unfollowed_ids  # don't unfollow if we already unfollowed
    log(user, '…of whom %d have not already been unfollowed', len(unfollow_ids))

    unfollow_ids -= keeper_ids  # don't unfollow keepers
    log(user, '…of whom %d are not keepers', len(unfollow_ids))

    log(user, 'unfollowing %d leaders and %d followers',
        len(unfollow_ids & unfollow_leader_ids), len(unfollow_ids & unfollow_follower_ids))
    for unfollow_id in unfollow_ids:
        unfollow(db, user, unfollow_id)

    with db, db.cursor() as cursor:
        mentor_follower_ids = {id for mentor in mentors
                               for id in database.get_twitter_follower_ids(cursor, mentor.id)}
        # reload since we unfollowed:
        leader_ids = database.get_twitter_leader_ids(cursor, user.twitter_id)
    log(user, '%d mentor followers', len(mentor_follower_ids))

    follow_ids = mentor_follower_ids - followed_ids - leader_ids
    log(user, '…of whom %d have not already been followed', len(follow_ids))

    max_leaders = max(int(len(follower_ids) * MAX_LEADER_RATIO), len(follower_ids) + EXTRA_LEADERS)
    log(user, '%d currently followed (max %d)', len(leader_ids), max_leaders)

    day_ago = now() - DAY
    follows_today = sum(1 for f in follows if f.time > day_ago)
    log(user, '%d already followed today (max %d)', follows_today, MAX_FOLLOWS_PER_DAY)
    remaining_follows_today = min(max_leaders - len(leader_ids),
                                  MAX_FOLLOWS_PER_DAY - follows_today)
    log(user, '%d remaining follows today', remaining_follows_today)
    if remaining_follows_today > 0:
        follow_ids = list(follow_ids)
        random.shuffle(follow_ids)
        for follow_id in follow_ids[:remaining_follows_today]:
            follow(db, user, follow_id)
            delay = random.uniform(1, 2) * FOLLOW_PERIOD
            log(user, 'sleeping for %s', delay)
            time.sleep(delay.total_seconds())


def run_forever(db, user):
    try:
        while True:
            run(db, user)
            delay = random.uniform(0.1, 0.9) * USER_FOLLOWERS_UPDATE_PERIOD
            log(user, 'sleeping for %s', delay)
            time.sleep(delay.total_seconds())
    except requests.exceptions.HTTPError as e:
        log(user, 'http error %d: %s', e.response.status_code, e.response.text, level=logging.ERROR)
        raise e


def main():
    db = database.connect()

    with db, db.cursor() as cursor:
        users = database.get_users(cursor)

    gevent.joinall([gevent.spawn(run_forever, db, user)
                    for user in users])


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    main()
