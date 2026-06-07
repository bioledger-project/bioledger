"""Tests for custom Jinja2 filters used in BioLedger command templates."""

import pytest

from bioledger.forges.analysisforge.executor import _jinja_env


class TestBasenameFilter:
    """Tests for the | basename Jinja2 filter."""

    def test_basename_simple_path(self):
        """basename extracts filename from simple path."""
        tmpl = _jinja_env.from_string('{{ path | basename }}')
        result = tmpl.render(path='/input/sample.fastq')
        assert result == 'sample.fastq'

    def test_basename_no_directory(self):
        """basename works with filename only (no directory)."""
        tmpl = _jinja_env.from_string('{{ path | basename }}')
        result = tmpl.render(path='sample.fastq')
        assert result == 'sample.fastq'

    def test_basename_nested_path(self):
        """basename extracts filename from deeply nested path."""
        tmpl = _jinja_env.from_string('{{ path | basename }}')
        result = tmpl.render(path='/data/samples/batch1/sample_R1.fastq.gz')
        assert result == 'sample_R1.fastq.gz'


class TestSplitextFilter:
    """Tests for the | splitext Jinja2 filter."""

    def test_splitext_fastq(self):
        """splitext separates root and .fastq extension."""
        tmpl = _jinja_env.from_string('{{ path | splitext | last }}')
        result = tmpl.render(path='/input/sample.fastq')
        assert result == '.fastq'

    def test_splitext_gzipped(self):
        """splitext handles .fastq.gz correctly (returns .gz)."""
        tmpl = _jinja_env.from_string('{{ path | splitext | last }}')
        result = tmpl.render(path='/input/sample.fastq.gz')
        # os.path.splitext returns '.gz' for 'sample.fastq.gz'
        assert result == '.gz'

    def test_splitext_no_extension(self):
        """splitext returns empty string for files without extension."""
        tmpl = _jinja_env.from_string('{{ path | splitext | last }}')
        result = tmpl.render(path='/input/sample')
        assert result == ''

    def test_splitext_full_result(self):
        """splitext returns [root, ext] list."""
        tmpl = _jinja_env.from_string('{{ path | splitext }}')
        result = tmpl.render(path='/input/sample.fastq')
        # Result is string representation of list: ['/input/sample', '.fastq']
        assert "'/input/sample'" in result and "'.fastq'" in result


class TestFilterChaining:
    """Tests for chaining multiple filters together."""

    def test_basename_then_splitext(self):
        """Common pattern: get filename then extract extension."""
        tmpl = _jinja_env.from_string('{{ path | basename | splitext | last }}')
        result = tmpl.render(path='/data/samples/sample.fastq')
        assert result == '.fastq'

    def test_basename_replace_pattern(self):
        """Galaxy-style: basename then replace spaces with underscores."""
        tmpl = _jinja_env.from_string("{{ path | basename | replace(' ', '_') }}")
        result = tmpl.render(path='/input/sample name with spaces.fastq')
        assert result == 'sample_name_with_spaces.fastq'


class TestGalaxyCommandTemplates:
    """Tests for Galaxy-converted command template patterns."""

    def test_galaxy_element_identifier_pattern(self):
        """Galaxy's $input.element_identifier → inputs.input_file | basename."""
        context = {
            'inputs': {'input_file': '/input/sample.fastq'},
            'outputs': {'_dir': '/output'}
        }
        # This is the pattern the LLM converts to
        tmpl = _jinja_env.from_string('fastqc {{ inputs.input_file | basename }}')
        result = tmpl.render(**context)
        assert 'fastqc sample.fastq' == result

    def test_galaxy_ext_pattern(self):
        """Galaxy's $input.ext → inputs.input_file | splitext | last."""
        context = {
            'inputs': {'input_file': '/input/sample.fastq'},
        }
        tmpl = _jinja_env.from_string(
            '{% if (inputs.input_file | splitext | last) == ".gz" %}gunzip{% endif %}'
        )
        result = tmpl.render(**context)
        assert 'gunzip' not in result  # .fastq is not .gz

    def test_galaxy_conditional_with_basename(self):
        """Complex Galaxy pattern: basename in conditional."""
        context = {
            'inputs': {'input_file': '/data/sample.bam'},
        }
        tmpl = _jinja_env.from_string(
            '{% if "bam" in (inputs.input_file | basename) %}samtools{% endif %}'
        )
        result = tmpl.render(**context)
        assert 'samtools' in result

    def test_stem_filter(self):
        """Galaxy's stem filter: get filename without extension."""
        context = {'inputs': {'input_file': '/input/sample.fastq.gz'}}
        tmpl = _jinja_env.from_string(
            'output/{{ inputs.input_file | stem }}.txt'
        )
        result = tmpl.render(**context)
        assert result == 'output/sample.fastq.txt'

    def test_stem_filter_simple(self):
        """Stem filter with simple filename."""
        context = {'inputs': {'input_file': 'sample.bam'}}
        tmpl = _jinja_env.from_string('{{ inputs.input_file | stem }}')
        result = tmpl.render(**context)
        assert result == 'sample'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
