"""
Created on 4/20/2016

@author: Azhar
"""
import logging
import os
import re
from collections import defaultdict, OrderedDict
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from mysql.connector import connection

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(process)d:%(name)s - %(levelname)s - %(message)s')
_log = logging.getLogger(__name__)


@lru_cache(maxsize=128)
def camelize(text):
    text = text.lower()
    return str(text[0].upper() +
               re.sub(r'_([a-z0-9])', lambda m: m.group(1).upper(), text[1:]))


@lru_cache(maxsize=128)
def plural(noun):
    rules = (
        ('[ml]ouse$', '([ml])ouse$', '\\1ice'),
        ('child$', 'child$', 'children'),
        ('booth$', 'booth$', 'booths'),
        ('foot$', 'foot$', 'feet'),
        ('ooth$', 'ooth$', 'eeth'),
        ('l[eo]af$', 'l([eo])af$', 'l\\1aves'),
        ('sis$', 'sis$', 'ses'),
        ('man$', 'man$', 'men'),
        ('ife$', 'ife$', 'ives'),
        ('eau$', 'eau$', 'eaux'),
        ('lf$', 'lf$', 'lves'),
        ('[sxz]$', '$', 'es'),
        ('[^aeioudgkprt]h$', '$', 'es'),
        ('(qu|[^aeiou])y$', 'y$', 'ies'),
        ('$', '$', 's')
    )

    def regexs():
        for rule in rules:
            pattern, search, replace = rule
            yield lambda word: re.search(pattern, word) and re.sub(search, replace, word)

    for regex in regexs():
        result = regex(noun)
        if result:
            return result


def main():
    namespace = r'App\Models'

    ignore = [
        'failed_jobs'
    ]

    db = {
        'user': 'root',
        'password': 'root',
        'host': '127.0.0.1',
        'database': 'epkbdb',
    }

    path = Path(r'D:\Users\Azhar\Projects\GPO-EPKB\models')
    _log.info('cleanup %s', path)
    for f in path.iterdir():
        if f.is_file():
            f.unlink()

    tables = defaultdict(lambda: {
        'key': 'id',
        'autoincrement': False,
        'column': OrderedDict(),
        'fillable': [],
        'date': [],
        'null': [],
        'parent': OrderedDict(),
        'child': OrderedDict(),
    })

    _log.info('connection')
    with closing(connection.MySQLConnection(**db)) as cnx:
        _log.info('loading table definition')
        with closing(cnx.cursor()) as cursor:
            cursor.execute('''\
SELECT TABLE_NAME AS `table`, COLUMN_NAME AS `column`, COLUMN_KEY AS `key`, IS_NULLABLE AS `null`, DATA_TYPE AS `type`,
  EXTRA AS `extra`
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
''')

            for table, column, key, null, type_, extra in cursor:
                if column not in ['id', 'created_at', 'updated_at', 'deleted_at']:
                    tables[table]['fillable'].append(column)

                if 'auto_increment' in extra:
                    tables[table]['autoincrement'] = True

                if key == 'PRI':
                    tables[table]['key'] = column

                if type_ in ['created_at',
                             'updated_at',
                             'deleted_at']:
                    type_ = '\Carbon\Carbon'

                elif type_ in ['date',
                               'datetime',
                               'time',
                               'timestamp']:
                    type_ = '\Carbon\Carbon'
                    tables[table]['date'].append(column)

                elif type_ in ['char',
                               'enum',
                               'longtext',
                               'mediumtext',
                               'set',
                               'text',
                               'tinytext',
                               'varchar']:
                    type_ = 'string'

                elif type_ in ['bigint',
                               'bit',
                               'int',
                               'mediumint',
                               'smallint',
                               'tinyint',
                               'year']:
                    type_ = 'integer'

                elif type_ in ['decimal',
                               'double',
                               'float',
                               'numeric',
                               'real']:
                    type_ = 'float'

                elif type_ in ['bool',
                               'boolean']:
                    type_ = 'boolean'

                else:
                    type_ = 'mixed'

                if null == 'YES':
                    tables[table]['null'].append(column)

                tables[table]['column'][column] = type_

        _log.info('loading table relation')
        with closing(cnx.cursor()) as cursor:
            cursor.execute('''\
SELECT TABLE_NAME AS `table`, COLUMN_NAME AS `column`,
  REFERENCED_TABLE_NAME AS `ref_table`, REFERENCED_COLUMN_NAME AS `ref_column`
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE()
  AND REFERENCED_TABLE_SCHEMA = DATABASE()
  AND REFERENCED_TABLE_NAME IS NOT NULL
''')

            for table, column, ref_table, ref_column in cursor:
                tables[table]['parent'][ref_table] = (column, ref_column)
                tables[ref_table]['child'][table] = (column, ref_column)

        for table, properties in tables.items():
            if table in ignore:
                continue

            _log.info('processing table %s', table)

            name = camelize(table)

            docs = []
            methods = []
            hidden = []

            # property
            columns = []
            for column, type_ in properties['column'].items():
                if column in properties['null']:
                    type_ = 'null|' + type_
                columns.append('@property {type} ${name}'.format(type=type_, name=column))

            if columns:
                docs.append('\n * '.join(columns))

            # relation
            columns = []
            for ref_table in properties['child']:
                model = camelize(ref_table)
                column, ref_column = properties['child'][ref_table]

                if column == tables[ref_table]['key']:
                    ref = model[0].lower() + model[1:]

                    columns.append('@property-read {model} ${ref}'.format(model=model, ref=ref))
                    methods.append('''\

    /**
     * @return \Illuminate\Database\Eloquent\Relations\HasOne|Builder
     */
    public function {ref}()
    {{
        return $this->hasOne('{namespace}\{model}', '{column}', '{ref_column}');
    }}
'''.format(ref=ref, namespace=namespace, model=model, column=column, ref_column=ref_column))

                else:
                    if column.startswith(properties['key']):
                        prefix = column.replace(properties['key'], '')
                        ref = camelize(ref_table + prefix)
                    else:
                        ref = model

                    ref = plural(ref[0].lower() + ref[1:])

                    columns.append('@property-read Collection|{model}[] ${ref}'.format(model=model, ref=ref))
                    methods.append('''\

    /**
     * @return \Illuminate\Database\Eloquent\Relations\HasMany|Builder
     */
    public function {ref}()
    {{
        return $this->hasMany('{namespace}\{model}', '{column}', '{ref_column}');
    }}
'''.format(ref=ref, namespace=namespace, model=model, column=column, ref_column=ref_column))

            for ref_table in properties['parent']:
                model = camelize(ref_table)
                column, ref_column = properties['parent'][ref_table]

                if column.startswith(tables[ref_table]['key']):
                    prefix = column.replace(tables[ref_table]['key'], '')
                    ref = camelize(ref_table + prefix)
                else:
                    ref = model

                ref = ref[0].lower() + ref[1:]

                columns.append('@property-read {model} ${ref}'.format(model=model, ref=ref))

                methods.append('''\

    /**
     * @return \Illuminate\Database\Eloquent\Relations\BelongsTo|Builder
     */
    public function {ref}()
    {{
        return $this->belongsTo('{namespace}\{model}', '{column}', '{ref_column}');
    }}
'''.format(ref=ref, namespace=namespace, model=model, column=column, ref_column=ref_column))

            if columns:
                docs.append('\n * '.join(columns))

            # helper method
            columns = []
            for column in properties['column'].keys():
                column = camelize(column)
                columns.append('@method static Builder|{name} where{method}($value)'.format(name=name, method=column))

            if columns:
                docs.append('\n * '.join(columns))

            if (table + '_salah') in tables:
                methods.insert(0, '''\

    protected $isSkipRevision = false;

    protected function saveRevision()
    {{
        if ($this->isSkipRevision)
            return;

        /* @var $Akun Akun */
        $Akun = \Auth::user();

        \DB::statement('INSERT INTO {table}_salah
SELECT NULL, CURRENT_TIMESTAMP(), {table}.*, :hid
FROM {table}
WHERE {key} = :id', [
            'hid' => $Akun ? $Akun->akun_id : null,
            'id' => $this->{key},
        ]);

        $this->isSkipRevision = true;
    }}

    public static function boot()
    {{
        parent::boot();

        static::updating(function ($Model) {{
            /* @type $Model {model} */
            $Model->saveRevision();
        }});

        static::deleting(function ($Model) {{
            /* @type $Model {model} */
            $Model->saveRevision();
        }});
    }}

    public function __construct(array $attributes = [])
    {{
        parent::__construct($attributes);

        $Model = $this;
        \Event::listen('Illuminate\Database\Events\Transaction*', function () use ($Model) {{
            $Model->isSkipRevision = false;
        }});
    }}
'''.format(table=table, key=properties['key'], model=name))

            docs = '\n *\n * '.join(docs)
            if docs:
                docs = '\n * ' + docs + '\n *'

            methods = ''.join(methods)

            incrementing = 'true' if properties['autoincrement'] else 'false'

            # fillable
            fillable = ["        '%s'" % column for column in properties['fillable']]
            fillable = ',\n'.join(fillable)
            if fillable:
                fillable = '\n' + fillable + '\n    '

            # dates
            dates = ["        '%s'" % column for column in properties['date']]
            dates = ',\n'.join(dates)
            if dates:
                dates = '\n' + dates + '\n    '

            # casts
            n = 0
            for column in properties['column']:
                if column in ['passwd']:
                    hidden.append("        '%s'" % column)

                if column in properties['date']:
                    continue

                if n < len(column):
                    n = len(column)

            casts = []
            for column, type_ in properties['column'].items():
                if column in properties['date']:
                    continue

                casts.append("        '{column}{pad}=> {type}'".format(
                    column=column,
                    pad=' ' * (n - len(column) + 1),
                    type=type_,
                ))

            hidden = ',\n'.join(hidden)
            if hidden:
                hidden = '\n' + hidden + '\n    '

            casts = ',\n'.join(casts)
            if casts:
                casts = '\n' + casts + '\n    '

            text = open(os.path.join(os.path.realpath(os.path.dirname(__file__)), 'template.txt')).read()
            text = text.format(
                namespace=namespace,
                name=name,
                docs=docs,
                table=table,
                key=properties['key'],
                incrementing=incrementing,
                hidden=hidden,
                fillable=fillable,
                dates=dates,
                casts=casts,
                methods=methods
            )

            f = path / (name + '.php')
            f.write_text(text)

    _log.info('done')

if __name__ == '__main__':
    main()
