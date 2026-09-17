#!/usr/bin/env python3
"""Validate the checked-in historical corpus; no network or third-party packages required.

The JSON is the curated source of truth. --check-source-cache DIR additionally
checks each Sanguozhi quotation against source excerpts collected with a browser.
This is a validator, not a claim that historical dates can be mechanically proved.
"""
import argparse
import collections
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def normalize(text):
    text = re.sub(r"cite\d+†(.*?)", lambda m: m[1].split('†')[0], text)
    return re.sub(r"\s+", "", text)


def validate(path, source_cache=None):
    people = json.loads(path.read_text())
    assert isinstance(people, list) and len(people) >= 150
    ids, names = set(), set()
    for person in people:
        name = person['name']
        assert re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', person['id']), name
        assert person['id'] not in ids and name not in names, name
        ids.add(person['id']); names.add(name)
        assert person['gender'] in {'男', '女'}, name
        assert set(person['factions']) <= {'东汉', '曹魏', '蜀汉', '孙吴', '西晋', '其他'}, name
        assert person['factions'] and person['roles'] and person['facts'], name
        assert isinstance(person['active_year'], int) and 184 <= person['active_year'] <= 316, name
        for field in ('birth_year', 'death_year'):
            assert person[field] is None or isinstance(person[field], int), (name, field)
        if person['birth_year'] is not None:
            assert person['birth_year'] <= person['active_year'], name
        if person['death_year'] is not None:
            assert person['death_year'] >= person['active_year'], name
        assert isinstance(person['aliases'], list) and all(isinstance(x, str) for x in person['aliases'])
        assert person['sources'] and all(x['url'].startswith('https://zh.wikisource.org/') for x in person['sources'])
        assert isinstance(person['sanguozhi'], bool), name
        evidence = person['sanguozhi_evidence']
        if person['sanguozhi']:
            assert evidence and evidence['quote'] and '三國志' in evidence['url'], name
            assert not any(bad in evidence['quote'] for bad in ('', '', '†', 'http', 'wikipedia', 'wikisource', '维基', 'Image:', '导航', '[Input]')), name
            assert not re.search(r'L\d+:', evidence['quote']), name
            assert any(mark in evidence['quote'] for mark in '，。；：'), name
            assert evidence['url'] in [s['url'] for s in person['sources']], name
            if source_cache:
                volume = int(evidence['url'].rsplit('卷', 1)[1])
                cache = Path(source_cache)
                text = '\n'.join(p.read_text() for p in [*cache.glob(f'san{volume}.txt'), *cache.glob(f'san{volume}-*.txt')])
                assert normalize(evidence['quote']) in normalize(text), (name, evidence['quote'])
        else:
            assert evidence is None, name
    assert any(p['death_year'] == 184 for p in people), '184 boundary missing'
    assert any(p['birth_year'] and p['birth_year'] > 280 and p['death_year'] and p['death_year'] > 316 for p in people), 'late boundary missing'
    assert sum(p['gender'] == '女' for p in people) >= 12, 'female coverage'
    result = {
        'people': len(people),
        'sanguozhi_verified': sum(p['sanguozhi'] for p in people),
        'women': sum(p['gender'] == '女' for p in people),
        'factions': dict(collections.Counter(f for p in people for f in p['factions'])),
        'source_pages': len({s['url'] for p in people for s in p['sources']}),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-source-cache', type=Path)
    args = parser.parse_args()
    validate(ROOT / 'data' / 'characters.json', args.check_source_cache)
