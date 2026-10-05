/*
 * Client-side reader for Codeforces Polygon packages (the "full" package zip).
 *
 * Used by the problem create page (statement + limits) and the test data page
 * (tests, subtasks, checker, interactor, grader, file IO). Everything runs in
 * the browser; the results are written into the existing Django forms, which
 * the problem setter reviews and submits as usual.
 *
 * Requires JSZip (resources/claoj/jszip).
 *
 * Package layout handled (paths are relative to problem.xml):
 *   problem.xml                          metadata, tests, groups, assets
 *   tests/01, tests/01.a ...             tests (see <input/answer-path-pattern>)
 *   files/check.cpp, check.cpp           checker source
 *   files/interactor.cpp                 interactor source (interactive problems)
 *   files/grader.cpp, files/*.h          grader + header (resources with for-types)
 *   statements/<lang>/problem-properties.json   statement sections in Polygon TeX
 *   statements/<lang>/*.png ...          images used by the statement
 */
(function (window) {
    'use strict';

    // Polygon standard checkers whose behaviour matches a built-in DMOJ checker
    // exactly. Everything else runs the package's own testlib source.
    var NATIVE_CHECKERS = {
        // DMOJ "standard" compares tokens and keeps line structure, like lcmp.
        'std::lcmp.cpp': {checker: 'standard'},
        // rcmpN: absolute or relative error 10^-N, same as DMOJ "floats".
        'std::rcmp4.cpp': {checker: 'floats', args: {precision: 4}},
        'std::rcmp6.cpp': {checker: 'floats', args: {precision: 6}},
        'std::rcmp9.cpp': {checker: 'floats', args: {precision: 9}},
    };

    var SECTION_TITLES = {
        english: {
            input: 'Input Specification', output: 'Output Specification', interaction: 'Interaction',
            scoring: 'Scoring', sampleInput: 'Sample Input {0}', sampleOutput: 'Sample Output {0}', notes: 'Notes',
        },
        vietnamese: {
            input: 'Dữ liệu vào', output: 'Kết quả', interaction: 'Tương tác',
            scoring: 'Chấm điểm', sampleInput: 'Dữ liệu vào mẫu {0}', sampleOutput: 'Kết quả mẫu {0}',
            notes: 'Ghi chú',
        },
    };

    function format(str) {
        var args = Array.prototype.slice.call(arguments, 1);
        return String(str).replace(/\{(\d+)\}/g, function (m, i) {
            return i < args.length ? args[i] : m;
        });
    }

    function basename(path) {
        return path.split('/').pop();
    }

    function dirname(path) {
        var i = path.lastIndexOf('/');
        return i < 0 ? '' : path.substring(0, i + 1);
    }

    // Polygon path patterns use printf syntax, e.g. "tests/%02d".
    function formatPattern(pattern, index) {
        return pattern.replace(/%(0?)(\d*)d/, function (m, zero, width) {
            var s = String(index);
            width = parseInt(width, 10) || 0;
            while (s.length < width)
                s = (zero ? '0' : ' ') + s;
            return s;
        });
    }

    function children(el, name) {
        if (!el)
            return [];
        return Array.prototype.filter.call(el.children, function (c) {
            return c.tagName === name;
        });
    }

    function child(el, name) {
        return children(el, name)[0] || null;
    }

    function text(el) {
        return el ? el.textContent.trim() : '';
    }

    function parseNumber(value) {
        if (value === null || value === undefined || value === '')
            return null;
        var n = parseFloat(value);
        return isNaN(n) ? null : n;
    }

    // Polygon source types look like "cpp.g++17", "cpp.gcc14-64-msys2-g++23".
    function judgeLanguage(sourceType) {
        return /(g\+\+|c\+\+)(20|23|2a|2b)/.test(sourceType || '') ? 'CPP20' : 'CPP17';
    }

    // The statement may not contain these characters
    // (settings.DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS).
    function normalizeQuotes(s) {
        return s.replace(/[“”]/g, '"').replace(/[‘’]/g, '\'');
    }

    /* ------------------------------------------------------------------ */
    /* Polygon TeX -> Markdown (the subset Polygon statements use)         */
    /* ------------------------------------------------------------------ */

    // Index of the "}" matching the "{" at text[open], or -1.
    function matchBrace(text, open) {
        var depth = 0;
        for (var i = open; i < text.length; i++) {
            var c = text[i];
            if (c === '\\') {
                i++;
                continue;
            }
            if (c === '{')
                depth++;
            else if (c === '}' && --depth === 0)
                return i;
        }
        return -1;
    }

    // Reads `count` brace groups after position `pos`, skipping an optional
    // [..] argument first. Returns {args, end} or null.
    function readArgs(text, pos, count) {
        var args = [];
        var i = pos;
        while (text[i] === ' ')
            i++;
        if (text[i] === '[') {
            var close = text.indexOf(']', i);
            if (close < 0)
                return null;
            i = close + 1;
        }
        for (var n = 0; n < count; n++) {
            while (text[i] === ' ' || text[i] === '\n')
                i++;
            if (text[i] !== '{')
                return null;
            var end = matchBrace(text, i);
            if (end < 0)
                return null;
            args.push(text.substring(i + 1, end));
            i = end + 1;
        }
        return {args: args, end: i};
    }

    // Set by texToMarkdown: stores finished Markdown (code, math) so later
    // text-mode replacements cannot touch it.
    var keep = function (s) {
        return s;
    };
    var restore = function (s) {
        return s;
    };

    function unescapeCode(s) {
        return s.replace(/\\([_{}$%&#])/g, '$1').replace(/~/g, ' ');
    }

    function inlineCode(s) {
        return keep('`' + unescapeCode(s) + '`');
    }

    var COMMANDS = {
        textbf: [1, function (a) { return '**' + a[0] + '**'; }],
        bf: [1, function (a) { return '**' + a[0] + '**'; }],
        textit: [1, function (a) { return '*' + a[0] + '*'; }],
        it: [1, function (a) { return '*' + a[0] + '*'; }],
        emph: [1, function (a) { return '*' + a[0] + '*'; }],
        textsl: [1, function (a) { return '*' + a[0] + '*'; }],
        underline: [1, function (a) { return '<u>' + a[0] + '</u>'; }],
        sout: [1, function (a) { return '<s>' + a[0] + '</s>'; }],
        t: [1, function (a) { return inlineCode(a[0]); }],
        tt: [1, function (a) { return inlineCode(a[0]); }],
        texttt: [1, function (a) { return inlineCode(a[0]); }],
        url: [1, function (a) { return '<' + a[0] + '>'; }],
        href: [2, function (a) { return '[' + a[1] + '](' + a[0] + ')'; }],
        includegraphics: [1, function (a) { return '\n\n![image](' + a[0].trim() + ')\n\n'; }],
        footnote: [1, function (a) { return ' (' + a[0] + ')'; }],
        epigraph: [2, function (a) { return '\n\n> ' + a[0] + '\n>\n> &mdash; ' + a[1] + '\n\n'; }],
        section: [1, function (a) { return '\n\n### ' + a[0] + '\n\n'; }],
        subsection: [1, function (a) { return '\n\n#### ' + a[0] + '\n\n'; }],
        subsubsection: [1, function (a) { return '\n\n##### ' + a[0] + '\n\n'; }],
        paragraph: [1, function (a) { return '\n\n**' + a[0] + '**\n\n'; }],
        caption: [1, function (a) { return '\n\n*' + a[0] + '*\n\n'; }],
        textcolor: [2, function (a) { return a[1]; }],
        colorbox: [2, function (a) { return a[1]; }],
    };

    // Commands whose single argument is kept as-is.
    ['textrm', 'textsf', 'textsc', 'textnormal', 'textup', 'mbox', 'text', 'hbox', 'textmd'].forEach(function (c) {
        COMMANDS[c] = [1, function (a) { return a[0]; }];
    });

    var SYMBOLS = {
        ldots: '…', dots: '…', cdots: '…', LaTeX: 'LaTeX', TeX: 'TeX', textbackslash: '\\\\',
        textless: '&lt;', textgreater: '&gt;', textasciitilde: '&#126;', textasciicircum: '^',
        quad: ' ', qquad: ' ', noindent: '', indent: '', centering: '', par: '\n\n', newline: '  \n',
        linebreak: '  \n', bigskip: '\n\n', medskip: '\n\n', smallskip: '\n\n', vspace: '', hspace: '',
        small: '', large: '', Large: '', LARGE: '', huge: '', Huge: '', normalsize: '', footnotesize: '',
        scriptsize: '', tiny: '', hline: '', clearpage: '', newpage: '', item: '',
    };

    function convertCommands(text) {
        var out = '';
        var i = 0;
        while (i < text.length) {
            var c = text[i];
            // {\bf text}, {\it text}, {\tt text}
            if (c === '{' && text[i + 1] === '\\') {
                var m = /^\{\\(bf|it|tt|em|sl|sc|rm)\b\s*/.exec(text.substring(i, i + 8));
                var end = m ? matchBrace(text, i) : -1;
                if (m && end > 0) {
                    var inner = convertCommands(text.substring(i + m[0].length, end));
                    var style = {bf: '**', it: '*', em: '*', sl: '*'}[m[1]];
                    if (m[1] === 'tt')
                        out += inlineCode(inner);
                    else
                        out += style ? style + inner + style : inner;
                    i = end + 1;
                    continue;
                }
            }
            if (c !== '\\') {
                out += c;
                i++;
                continue;
            }
            var next = text[i + 1];
            if (next === undefined) {
                i++;
                continue;
            }
            // Escaped characters and spacing commands.
            if (next === '\\') {
                out += '  \n';
                i += 2;
                continue;
            }
            if ('%&{}_#'.indexOf(next) >= 0) {
                out += next === '_' || next === '#' ? '\\' + next : next;
                i += 2;
                continue;
            }
            if (next === '$') {
                // A literal dollar sign; MathJax treats an unescaped $ as math.
                out += '\\$';
                i += 2;
                continue;
            }
            if (',;:! '.indexOf(next) >= 0) {
                out += ' ';
                i += 2;
                continue;
            }
            var name = /^[a-zA-Z]+\*?/.exec(text.substring(i + 1));
            if (!name) {
                out += next;
                i += 2;
                continue;
            }
            var cmd = name[0].replace(/\*$/, '');
            var after = i + 1 + name[0].length;
            if (COMMANDS.hasOwnProperty(cmd)) {
                var parsed = readArgs(text, after, COMMANDS[cmd][0]);
                if (parsed) {
                    out += COMMANDS[cmd][1](parsed.args.map(convertCommands));
                    i = parsed.end;
                    continue;
                }
            }
            if (SYMBOLS.hasOwnProperty(cmd)) {
                out += SYMBOLS[cmd];
                i = after;
                // \vspace{1em}, \hspace*{..}: drop the argument too.
                if (cmd === 'vspace' || cmd === 'hspace') {
                    var dropped = readArgs(text, after, 1);
                    if (dropped)
                        i = dropped.end;
                }
                continue;
            }
            // Unknown command: keep its first argument if it has one, drop the name.
            var unknown = readArgs(text, after, 1);
            if (unknown) {
                out += convertCommands(unknown.args[0]);
                i = unknown.end;
            } else {
                i = after;
            }
        }
        return out;
    }

    function convertTable(body) {
        var rows = body.replace(/\\(hline|cline\{[^}]*\})/g, '').split(/\\\\/).map(function (row) {
            return row.trim();
        }).filter(Boolean).map(function (row) {
            return row.split(/(?<!\\)&/).map(function (cell) {
                // Math in a cell may contain "|", which would end the cell.
                return restore(convertCommands(cell)).replace(/\n+/g, ' ').replace(/\|/g, '\\|').trim();
            });
        });
        if (!rows.length)
            return '';
        var width = Math.max.apply(null, rows.map(function (r) { return r.length; }));
        var line = function (cells) {
            while (cells.length < width)
                cells.push('');
            return '| ' + cells.join(' | ') + ' |';
        };
        var sep = '|' + new Array(width + 1).join(' --- |');
        return '\n\n' + [line(rows[0]), sep].concat(rows.slice(1).map(line)).join('\n') + '\n\n';
    }

    function convertLists(text) {
        var re = /\\begin\{(itemize|enumerate)\}((?:(?!\\begin\{(?:itemize|enumerate)\})[\s\S])*?)\\end\{\1\}/;
        var m;
        while ((m = re.exec(text))) {
            var ordered = m[1] === 'enumerate';
            var items = m[2].split(/\\item\b/).slice(1);
            var list = items.map(function (item, k) {
                var marker = ordered ? (k + 1) + '. ' : '- ';
                var indent = new Array(marker.length + 1).join(' ');
                var lines = item.trim().replace(/\n\s*\n/g, '\n').split('\n');
                return marker + lines.map(function (l, j) {
                    return j ? indent + l.trim() : l;
                }).join('\n');
            }).join('\n');
            text = text.substring(0, m.index) + '\n\n' + list + '\n\n' + text.substring(m.index + m[0].length);
        }
        return text;
    }

    function texToMarkdown(tex) {
        if (!tex)
            return '';
        var stash = [];
        keep = function (s) {
            stash.push(s);
            return '\u0000' + (stash.length - 1) + '\u0000';
        };
        // Stashed pieces can contain other stashed pieces (math inside a table).
        restore = function (s) {
            while (/\u0000\d+\u0000/.test(s)) {
                s = s.replace(/\u0000(\d+)\u0000/g, function (m, i) {
                    return stash[i];
                });
            }
            return s;
        };

        var text = tex.replace(/\r\n?/g, '\n');
        // Comments (an unescaped % to end of line).
        text = text.replace(/(^|[^\\])%.*$/gm, '$1');
        // Verbatim blocks and \verb.
        text = text.replace(/\\begin\{(verbatim|lstlisting)\}(?:\[[^\]]*\])?\n?([\s\S]*?)\\end\{\1\}/g, function (m, env, body) {
            return keep('\n\n```\n' + body.replace(/\n$/, '') + '\n```\n\n');
        });
        text = text.replace(/\\verb([^a-zA-Z\s])([\s\S]*?)\1/g, function (m, d, body) {
            return keep('`' + body + '`');
        });
        // Math. Inline math uses ~...~ on this site; display math uses $$...$$.
        text = text.replace(/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]/g, function (m, a, b) {
            return keep('\n\n$$' + (a || b).trim() + '$$\n\n');
        });
        text = text.replace(/(^|[^\\])\$((?:\\.|[^$\\])+?)\$/g, function (m, pre, body) {
            return pre + keep('~' + body.trim() + '~');
        });
        text = text.replace(/\\\(([\s\S]+?)\\\)/g, function (m, body) {
            return keep('~' + body.trim() + '~');
        });

        text = convertLists(text);
        text = text.replace(/\\begin\{(tabular\*?|array)\}(?:\{[^}]*\})*([\s\S]*?)\\end\{\1\}/g, function (m, env, body) {
            return keep(convertTable(body));
        });
        text = text.replace(/\\begin\{center\}([\s\S]*?)\\end\{center\}/g, '\n\n<center>\n\n$1\n\n</center>\n\n');
        text = text.replace(/\\begin\{(quote|quotation)\}([\s\S]*?)\\end\{\1\}/g, function (m, env, body) {
            return '\n\n' + body.trim().split('\n').map(function (l) { return '> ' + l; }).join('\n') + '\n\n';
        });
        // Any other environment (figure, table, minipage, ...): keep the content.
        text = text.replace(/\\(begin|end)\{[^}]*\}(\{[^}]*\}|\[[^\]]*\])*/g, '\n');

        text = convertCommands(text);
        // Text-mode ligatures and spacing. "~" is a math delimiter here, so a
        // TeX non-breaking space must not survive as "~".
        text = text.replace(/~/g, ' ')
            .replace(/---/g, '&mdash;').replace(/--/g, '&ndash;')
            .replace(/``|''/g, '"');

        text = restore(text);
        // Blank lines become empty; two trailing spaces (a TeX "\\" line break)
        // are kept as a Markdown hard break, any other trailing space is dropped.
        text = text.split('\n').map(function (line) {
            if (!line.trim())
                return '';
            return line.replace(/[ \t]+$/, function (m) {
                return m.length >= 2 ? '  ' : '';
            });
        }).join('\n');
        return normalizeQuotes(text.replace(/\n{3,}/g, '\n\n').trim());
    }

    /* ------------------------------------------------------------------ */
    /* Package                                                             */
    /* ------------------------------------------------------------------ */

    function PolygonPackage(zip, root, xml) {
        this.zip = zip;
        this.root = root;  // folder containing problem.xml inside the zip
        this.warnings = [];
        this._parse(xml);
    }

    PolygonPackage.load = function (file, messages) {
        return JSZip.loadAsync(file).then(function (zip) {
            var xmlPath = Object.keys(zip.files).filter(function (p) {
                return basename(p) === 'problem.xml' && !zip.files[p].dir;
            }).sort(function (a, b) {
                return a.length - b.length;
            })[0];
            if (!xmlPath)
                throw new Error(messages.noProblemXml);
            return zip.file(xmlPath).async('string').then(function (xmlText) {
                var xml = new DOMParser().parseFromString(xmlText, 'application/xml');
                if (xml.getElementsByTagName('parsererror').length || !xml.documentElement ||
                    xml.documentElement.tagName !== 'problem')
                    throw new Error(messages.badProblemXml);
                return new PolygonPackage(zip, dirname(xmlPath), xml.documentElement);
            });
        });
    };

    PolygonPackage.texToMarkdown = texToMarkdown;
    PolygonPackage.formatPattern = formatPattern;
    PolygonPackage.format = format;

    PolygonPackage.prototype.file = function (path) {
        return path ? this.zip.file(this.root + path) : null;
    };

    PolygonPackage.prototype.warn = function (key) {
        var args = Array.prototype.slice.call(arguments, 1);
        var duplicate = this.warnings.some(function (w) {
            return w.key === key && w.args.join('\u0000') === args.join('\u0000');
        });
        if (!duplicate)
            this.warnings.push({key: key, args: args});
    };

    PolygonPackage.prototype._parse = function (problem) {
        var self = this;
        this.shortName = problem.getAttribute('short-name') || '';
        this.url = problem.getAttribute('url') || '';

        this.names = {};
        children(child(problem, 'names'), 'name').forEach(function (n) {
            self.names[n.getAttribute('language')] = n.getAttribute('value') || '';
        });

        this.statements = children(child(problem, 'statements'), 'statement').map(function (s) {
            return {language: s.getAttribute('language'), path: s.getAttribute('path'), type: s.getAttribute('type')};
        });

        var judging = child(problem, 'judging');
        this.inputFile = judging ? judging.getAttribute('input-file') || '' : '';
        this.outputFile = judging ? judging.getAttribute('output-file') || '' : '';

        var testsets = children(judging, 'testset');
        var testset = testsets.filter(function (t) {
            return t.getAttribute('name') === 'tests';
        })[0] || testsets[0] || null;
        this.otherTestsets = testsets.filter(function (t) {
            return t !== testset;
        }).map(function (t) {
            return t.getAttribute('name');
        });
        this.testset = testset && this._parseTestset(testset);

        var files = child(problem, 'files');
        var assets = child(problem, 'assets');

        var checker = child(assets, 'checker');
        this.checker = checker && {
            name: checker.getAttribute('name'),  // e.g. "std::lcmp.cpp"; absent for a custom checker
            type: checker.getAttribute('type'),
            source: child(checker, 'source') && child(checker, 'source').getAttribute('path'),
            sourceType: child(checker, 'source') && child(checker, 'source').getAttribute('type'),
        };

        var interactor = child(assets, 'interactor');
        this.interactor = interactor && child(interactor, 'source') && {
            source: child(interactor, 'source').getAttribute('path'),
            sourceType: child(interactor, 'source').getAttribute('type'),
        };

        // Graders are resources compiled together with the contestant's
        // solution: <file for-types="cpp.*" ...><assets><asset name="solution"/>.
        var resources = children(child(files, 'resources'), 'file');
        var graders = resources.filter(function (f) {
            return children(child(f, 'assets'), 'asset').some(function (a) {
                return a.getAttribute('name') === 'solution';
            });
        });
        var headers = resources.filter(function (f) {
            var path = f.getAttribute('path') || '';
            return /\.h(pp)?$/.test(path) && basename(path) !== 'testlib.h';
        });
        this.graders = graders.map(function (f) {
            return {path: f.getAttribute('path'), forTypes: f.getAttribute('for-types') || '',
                    type: f.getAttribute('type') || ''};
        });
        this.headers = headers.map(function (f) {
            return f.getAttribute('path');
        });

        this.tags = children(child(problem, 'tags'), 'tag').map(function (t) {
            return t.getAttribute('value');
        });
    };

    PolygonPackage.prototype._parseTestset = function (el) {
        var groups = {};
        children(child(el, 'groups'), 'group').forEach(function (g) {
            groups[g.getAttribute('name')] = {
                name: g.getAttribute('name'),
                points: parseNumber(g.getAttribute('points')),
                pointsPolicy: g.getAttribute('points-policy') || 'complete-group',
                feedbackPolicy: g.getAttribute('feedback-policy') || '',
                dependencies: children(child(g, 'dependencies'), 'dependency').map(function (d) {
                    return d.getAttribute('group');
                }),
            };
        });
        return {
            name: el.getAttribute('name'),
            timeLimit: parseNumber(text(child(el, 'time-limit'))),  // ms
            memoryLimit: parseNumber(text(child(el, 'memory-limit'))),  // bytes
            inputPattern: text(child(el, 'input-path-pattern')),
            answerPattern: text(child(el, 'answer-path-pattern')),
            groups: groups,
            tests: children(child(el, 'tests'), 'test').map(function (t, i) {
                return {
                    index: i + 1,
                    method: t.getAttribute('method') || 'manual',
                    cmd: t.getAttribute('cmd') || '',
                    sample: t.getAttribute('sample') === 'true',
                    points: parseNumber(t.getAttribute('points')),
                    group: t.getAttribute('group') || '',
                    description: t.getAttribute('description') || '',
                };
            }),
        };
    };

    PolygonPackage.prototype.isScored = function () {
        var ts = this.testset;
        if (!ts)
            return false;
        return ts.tests.some(function (t) {
            return t.points > 0;
        }) || Object.keys(ts.groups).some(function (g) {
            return ts.groups[g].points > 0;
        });
    };

    // Fields of the "create problem" form. Limits use the site's units:
    // seconds and KiB (Polygon uses ms and bytes).
    PolygonPackage.prototype.problemFields = function (preferredLanguages) {
        var ts = this.testset;
        var language = this.pickLanguage(Object.keys(this.names), preferredLanguages);
        return {
            code: this.shortName.toLowerCase().replace(/[^a-z0-9_]/g, '_').substring(0, 32),
            name: language ? this.names[language] : '',
            timeLimit: ts && ts.timeLimit !== null ? ts.timeLimit / 1000 : null,
            memoryLimit: ts && ts.memoryLimit !== null ? Math.floor(ts.memoryLimit / 1024) : null,
            partial: this.isScored(),
        };
    };

    PolygonPackage.prototype.pickLanguage = function (languages, preferred) {
        preferred = preferred || [];
        for (var i = 0; i < preferred.length; i++) {
            if (languages.indexOf(preferred[i]) >= 0)
                return preferred[i];
        }
        return languages[0] || null;
    };

    // Statement as Markdown. Resolves to null when the package has none.
    // Images are returned separately so the page can upload them.
    PolygonPackage.prototype.statement = function (preferredLanguages) {
        var self = this;
        var languages = this.statements.filter(function (s) {
            return s.type === 'application/x-tex';
        }).map(function (s) {
            return s.language;
        });
        // Packages always ship statements/<lang>/problem-properties.json next to problem.tex.
        Object.keys(this.zip.files).forEach(function (p) {
            var m = /^statements\/([^/.][^/]*)\/problem-properties\.json$/.exec(p.substring(self.root.length));
            if (m && languages.indexOf(m[1]) < 0)
                languages.push(m[1]);
        });
        var language = this.pickLanguage(languages, preferredLanguages);
        if (!language)
            return Promise.resolve(null);

        var folder = 'statements/' + language + '/';
        var props = this.file(folder + 'problem-properties.json');
        if (!props)
            return Promise.resolve(null);

        return props.async('string').then(function (json) {
            var p = JSON.parse(json);
            var titles = SECTION_TITLES[language] || SECTION_TITLES.english;
            var parts = [texToMarkdown(p.legend)];
            var section = function (title, body) {
                if (body && body.trim())
                    parts.push('## ' + title + '\n\n' + texToMarkdown(body));
            };
            section(titles.input, p.input);
            section(titles.output, p.output);
            section(titles.interaction, p.interaction);
            section(titles.scoring, p.scoring);
            (p.sampleTests || []).forEach(function (sample, i) {
                var block = function (s) {
                    return '```\n' + String(s || '').replace(/\r\n?/g, '\n').replace(/\n+$/, '') + '\n```';
                };
                parts.push('## ' + format(titles.sampleInput, i + 1) + '\n\n' + block(sample.input));
                parts.push('## ' + format(titles.sampleOutput, i + 1) + '\n\n' + block(sample.output));
            });
            section(titles.notes, p.notes);

            var markdown = parts.filter(Boolean).join('\n\n') + '\n';
            var images = [];
            markdown.replace(/!\[image\]\(([^)\s]+)\)/g, function (m, path) {
                if (images.indexOf(path) < 0)
                    images.push(path);
            });
            return {
                language: language,
                otherLanguages: languages.filter(function (l) { return l !== language; }),
                name: p.name || self.names[language] || '',
                markdown: markdown,
                tutorial: p.tutorial ? texToMarkdown(p.tutorial) : '',
                images: images.map(function (path) {
                    return {path: path, file: self.file(folder + path)};
                }),
            };
        });
    };

    function testFileNames(count) {
        var width = Math.max(2, String(count).length);
        return function (index) {
            var id = String(index);
            while (id.length < width)
                id = '0' + id;
            return {input: id + '.inp', output: id + '.out'};
        };
    }

    // Everything the test data page needs. Rows follow the site's case table:
    // type C (case), S (batch start, holds the points), E (batch end).
    PolygonPackage.prototype.testData = function (messages) {
        var self = this;
        var ts = this.testset;
        if (!ts || !ts.tests.length)
            return Promise.reject(new Error(messages.noTests));

        if (this.otherTestsets.length)
            this.warn('otherTestsets', this.otherTestsets.join(', '), ts.name);

        // Tests must be present in the zip. Polygon's "standard" packages only
        // contain manual tests; the "full" package also has generated ones.
        var missing = [];
        var names = testFileNames(ts.tests.length);
        var out = new JSZip();
        ts.tests.forEach(function (t) {
            var input = self.file(formatPattern(ts.inputPattern, t.index));
            var answer = self.file(formatPattern(ts.answerPattern, t.index));
            if (!input || !answer) {
                missing.push(t.index);
                return;
            }
            out.file(names(t.index).input, input.async('uint8array'));
            out.file(names(t.index).output, answer.async('uint8array'));
        });
        if (missing.length)
            return Promise.reject(new Error(format(messages.missingTests, missing.slice(0, 10).join(', ') +
                                                   (missing.length > 10 ? ', ...' : ''))));

        var rows = this._caseRows(names);
        var checker = this._checker();
        var grader = this._grader();
        var files = [checker.file, grader.files.grader, grader.files.header].filter(Boolean);

        return Promise.all(files.map(function (f) {
            return f.zipEntry.async('blob').then(function (blob) {
                f.blob = blob;
            });
        })).then(function () {
            // Level 1: tests can be hundreds of MB and this runs on the page's thread.
            return out.generateAsync({type: 'blob', compression: 'DEFLATE', compressionOptions: {level: 1}});
        }).then(function (blob) {
            return {
                zip: blob,
                files: Object.keys(out.files).sort(),
                rows: rows,
                checker: checker,
                grader: grader,
                warnings: self.warnings,
            };
        });
    };

    PolygonPackage.prototype._caseRows = function (names) {
        var self = this;
        var ts = this.testset;
        var rows = [];
        var groupNames = Object.keys(ts.groups);

        if (!this.isScored()) {
            // ICPC-style package: no points anywhere. One point per test, like
            // the page's "Autofill testcases" button.
            if (groupNames.length)
                this.warn('groupsUnscored');
            ts.tests.forEach(function (t) {
                rows.push({type: 'C', input: names(t.index).input, output: names(t.index).output, points: 1});
            });
            return rows;
        }

        var round = function (points) {
            var value = Math.round(points || 0);
            if (points && value !== points)
                self.warn('pointsRounded', points, value);
            return value;
        };
        var done = {};
        ts.tests.forEach(function (t, position) {
            var group = t.group && ts.groups[t.group];
            if (!group || group.pointsPolicy !== 'complete-group') {
                rows.push({type: 'C', input: names(t.index).input, output: names(t.index).output,
                           points: round(t.points)});
                return;
            }
            if (done[t.group])
                return;
            done[t.group] = true;

            var members = ts.tests.filter(function (u) {
                return u.group === t.group;
            });
            var contiguous = members.every(function (u, k) {
                return ts.tests[position + k] === u;
            });
            if (!contiguous)
                self.warn('groupReordered', t.group);
            if (group.dependencies.length)
                self.warn('groupDependencies', t.group, group.dependencies.join(', '));

            var points = group.points !== null ? group.points : members.reduce(function (sum, u) {
                return sum + (u.points || 0);
            }, 0);
            rows.push({type: 'S', points: round(points), group: t.group});
            members.forEach(function (u) {
                rows.push({type: 'C', input: names(u.index).input, output: names(u.index).output});
            });
            rows.push({type: 'E'});
        });
        return rows;
    };

    // Checker for the test data form: {checker, args, file}.
    PolygonPackage.prototype._checker = function () {
        var c = this.checker;
        if (!c) {
            this.warn('noChecker');
            return {checker: 'standard', args: null, file: null};
        }
        // An interactive problem's checker reads the interactor's output, which
        // the judge only does for a custom checker, so keep Polygon's checker.
        var native = !this.interactor && NATIVE_CHECKERS[c.name];
        if (native)
            return {checker: native.checker, args: native.args || null, file: null, polygonName: c.name};

        if (c.type !== 'testlib')
            this.warn('checkerNotTestlib', c.type);
        var entry = this.file(c.source) || this.file('check.cpp');
        if (!entry || !/\.cpp$/i.test(entry.name)) {
            this.warn('checkerMissing', c.source || '');
            return {checker: 'standard', args: null, file: null};
        }
        var name = basename(entry.name);
        return {
            checker: 'bridged',
            args: {files: name, lang: judgeLanguage(c.sourceType), type: 'testlib'},
            file: {name: name, zipEntry: entry},
            polygonName: c.name || '',
        };
    };

    // Grader for the test data form: {grader, args, files: {grader, header}}.
    PolygonPackage.prototype._grader = function () {
        var args = {};
        var files = {grader: null, header: null};
        var grader = 'standard';

        if (this.interactor) {
            var interactor = this.file(this.interactor.source);
            if (interactor && /\.cpp$/i.test(interactor.name)) {
                grader = 'interactive';
                files.grader = {name: basename(interactor.name), zipEntry: interactor};
            } else {
                this.warn('interactorMissing', this.interactor.source || '');
            }
        } else if (this.graders.length) {
            var cpp = this.graders.filter(function (g) {
                return /\.cpp$/i.test(g.path);
            });
            var entry = cpp.length && this.file(cpp[0].path);
            var header = this.headers.length && this.file(this.headers[0]);
            if (entry && header) {
                grader = 'signature';
                files.grader = {name: basename(entry.name), zipEntry: entry};
                files.header = {name: basename(header.name), zipEntry: header};
                if (this.graders.length > 1 || this.headers.length > 1)
                    this.warn('multipleGraders', basename(entry.name), basename(header.name));
            } else {
                this.warn('graderUnsupported');
            }
        }

        var input = this.inputFile, output = this.outputFile;
        var isStd = function (f) {
            return !f || f === 'stdin' || f === 'stdout';
        };
        if (!isStd(input) || !isStd(output)) {
            if (grader !== 'standard')
                this.warn('fileIoIgnored', input || 'stdin', output || 'stdout');
            else if (isStd(input) || isStd(output))
                this.warn('fileIoPartial', input || 'stdin', output || 'stdout');
            else
                args = {io_method: 'file', io_input_file: input, io_output_file: output};
        }
        return {grader: grader, args: args, files: files};
    };

    window.PolygonPackage = PolygonPackage;
})(window);
