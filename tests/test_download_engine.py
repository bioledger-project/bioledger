"""Tests for the accelerated download engine in bioledger_isatab_schema.download.

Coverage:
- Shared verify path (_verify_and_record)
- Resume: partial file re-hashing, Range header, 200 restart
- Segmented download: N-part concat produces correct bytes
- Cross-file concurrency + aggregate error on partial failure
- SRA-Toolkit opt-in path (mocked subprocesses)
- aria2c opt-in path (mocked subprocess)
- Backward-compat: existing call signatures still work
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from bioledger_isatab_schema.dataset import DataFile, DataSet
from bioledger_isatab_schema.download import (
    _download_single_stream,
    _extract_sra_accession,
    _hash_file,
    _verify_and_record,
    download_remote_files,
    manifest_to_datafiles,
)
from bioledger_isatab_schema.manifest import Manifest, ManifestFile, StudyType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONTENT = b"ATCGATCGATCG" * 1000  # 12 000 bytes — content for fake FASTQ

def _checksums(data: bytes) -> tuple[str, str]:
    return hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()

def _make_file(content: bytes, *, sra_accession: str | None = None) -> DataFile:
    sha, md5 = _checksums(content)
    return DataFile(
        location="https://example.com/file.fastq.gz",
        format="fastq",
        is_remote=True,
        sha256=sha,
        md5=md5,
        expected_filename="file.fastq.gz",
        sra_accession=sra_accession,
    )


def _make_dataset(content: bytes, **file_kwargs: object) -> DataSet:
    return DataSet(
        name="test",
        files=[_make_file(content, **file_kwargs)],
    )


# ---------------------------------------------------------------------------
# _verify_and_record
# ---------------------------------------------------------------------------

class TestVerifyAndRecord:
    def test_passes_matching_sha256(self, tmp_path: Path) -> None:
        content = b"hello"
        sha, md5 = _checksums(content)
        path = tmp_path / "f"
        path.write_bytes(content)
        f = DataFile(location="x", sha256=sha, md5=None)
        _verify_and_record(f, path, sha, md5, len(content))
        assert f.downloaded_path == str(path)
        assert f.size_bytes == len(content)

    def test_raises_on_sha256_mismatch(self, tmp_path: Path) -> None:
        content = b"hello"
        sha, md5 = _checksums(content)
        path = tmp_path / "f"
        path.write_bytes(content)
        f = DataFile(location="x", sha256="deadbeef" * 8, md5=None)
        with pytest.raises(ValueError, match="sha256 mismatch"):
            _verify_and_record(f, path, sha, md5, len(content))
        assert not path.exists()

    def test_raises_on_md5_mismatch(self, tmp_path: Path) -> None:
        content = b"hello"
        sha, md5 = _checksums(content)
        path = tmp_path / "f"
        path.write_bytes(content)
        f = DataFile(location="x", sha256=None, md5="badmd5" + "0" * 26)
        with pytest.raises(ValueError, match="md5 mismatch"):
            _verify_and_record(f, path, sha, md5, len(content))

    def test_skip_md5_bypasses_md5_check(self, tmp_path: Path) -> None:
        content = b"hello"
        sha, md5 = _checksums(content)
        path = tmp_path / "f"
        path.write_bytes(content)
        f = DataFile(location="x", sha256=None, md5="wrong_md5")
        # Should NOT raise even with wrong md5 when skip_md5=True
        _verify_and_record(f, path, sha, md5, len(content), skip_md5=True)
        assert f.downloaded_path == str(path)

    def test_checksum_stored_on_file(self, tmp_path: Path) -> None:
        content = b"data"
        sha, md5 = _checksums(content)
        path = tmp_path / "f"
        path.write_bytes(content)
        f = DataFile(location="x", sha256=sha)
        _verify_and_record(f, path, sha, md5, len(content))
        assert f.sha256 == sha
        assert f.md5 == md5
        assert f.size_bytes == len(content)


# ---------------------------------------------------------------------------
# _hash_file
# ---------------------------------------------------------------------------

class TestHashFile:
    def test_hash_matches_content(self, tmp_path: Path) -> None:
        content = b"ATCG" * 500
        path = tmp_path / "data"
        path.write_bytes(content)
        sha, md5, size = _hash_file(path)
        assert sha == hashlib.sha256(content).hexdigest()
        assert md5 == hashlib.md5(content).hexdigest()
        assert size == len(content)


# ---------------------------------------------------------------------------
# Resume via _download_single_stream
# ---------------------------------------------------------------------------

class TestSingleStreamResume:
    """Test the resume branch: partial file → re-hash → Range header."""

    def _make_mock_transport(
        self,
        content: bytes,
        *,
        supports_range: bool = True,
        status_override: int | None = None,
    ) -> httpx.MockTransport:
        """Build an httpx MockTransport that serves byte-range responses."""

        def handler(request: httpx.Request) -> httpx.Response:
            range_hdr = request.headers.get("range", "")
            if range_hdr.startswith("bytes=") and supports_range:
                start = int(range_hdr.split("=")[1].split("-")[0])
                body = content[start:]
                return httpx.Response(
                    206,
                    content=body,
                    headers={"Content-Length": str(len(body))},
                )
            return httpx.Response(
                status_override or 200,
                content=content,
                headers={"Content-Length": str(len(content))},
            )

        return httpx.MockTransport(handler)

    @pytest.mark.asyncio
    async def test_full_download_no_partial(self, tmp_path: Path) -> None:
        content = CONTENT
        sha_expected, md5_expected = _checksums(content)
        path = tmp_path / "file.fastq.gz"
        transport = self._make_mock_transport(content)

        async with httpx.AsyncClient(transport=transport) as client:
            from bioledger_isatab_schema.download import _ProgressCtx
            with _ProgressCtx() as p:
                sha, md5, size = await _download_single_stream(
                    client, "https://example.com/file.fastq.gz", path, "file.fastq.gz", p, None
                )

        assert sha == sha_expected
        assert md5 == md5_expected
        assert size == len(content)
        assert path.read_bytes() == content

    @pytest.mark.asyncio
    async def test_resume_appends_and_rehashes(self, tmp_path: Path) -> None:
        content = CONTENT
        partial = content[:5000]
        sha_expected, md5_expected = _checksums(content)
        path = tmp_path / "file.fastq.gz"
        path.write_bytes(partial)  # pre-existing partial

        transport = self._make_mock_transport(content, supports_range=True)

        async with httpx.AsyncClient(transport=transport) as client:
            from bioledger_isatab_schema.download import _ProgressCtx
            with _ProgressCtx() as p:
                sha, md5, size = await _download_single_stream(
                    client, "https://example.com/file.fastq.gz", path, "file.fastq.gz", p, None
                )

        assert sha == sha_expected
        assert path.read_bytes() == content

    @pytest.mark.asyncio
    async def test_resume_restarts_when_server_ignores_range(self, tmp_path: Path) -> None:
        """When the server returns 200 for a range request, we restart from scratch."""
        content = CONTENT
        partial = content[:3000]
        path = tmp_path / "file.fastq.gz"
        path.write_bytes(partial)

        # Server always returns 200 (ignores range)
        transport = self._make_mock_transport(content, supports_range=False, status_override=200)

        async with httpx.AsyncClient(transport=transport) as client:
            from bioledger_isatab_schema.download import _ProgressCtx
            with _ProgressCtx() as p:
                sha, md5, size = await _download_single_stream(
                    client, "https://example.com/file.fastq.gz", path, "file.fastq.gz", p, None
                )

        # Full content should be there, not partial+partial
        assert size == len(content)
        assert path.read_bytes() == content


# ---------------------------------------------------------------------------
# Segmented download
# ---------------------------------------------------------------------------

class TestSegmentedDownload:
    @pytest.mark.asyncio
    async def test_segmented_produces_correct_bytes(self, tmp_path: Path) -> None:
        """Segmented download assembles the same bytes as a sequential download."""
        from bioledger_isatab_schema.download import _download_segmented, _ProgressCtx

        content = CONTENT
        sha_expected, md5_expected = _checksums(content)

        def handler(request: httpx.Request) -> httpx.Response:
            range_hdr = request.headers.get("range", "")
            if range_hdr.startswith("bytes="):
                parts = range_hdr.split("=")[1].split("-")
                start, end = int(parts[0]), int(parts[1])
                return httpx.Response(
                    206,
                    content=content[start:end + 1],
                    headers={"Content-Length": str(end - start + 1)},
                )
            return httpx.Response(200, content=content)

        transport = httpx.MockTransport(handler)
        path = tmp_path / "out.fastq.gz"

        async with httpx.AsyncClient(transport=transport) as client:
            with _ProgressCtx() as p:
                task = p.add_task("test", total=len(content))
                sha, md5, size = await _download_segmented(
                    client, "https://example.com/out.fastq.gz",
                    path, len(content), "out.fastq.gz", p, task, n_segments=4,
                )

        assert sha == sha_expected
        assert md5 == md5_expected
        assert size == len(content)
        assert path.read_bytes() == content

    @pytest.mark.asyncio
    async def test_parts_cleaned_up_on_success(self, tmp_path: Path) -> None:
        from bioledger_isatab_schema.download import _download_segmented, _ProgressCtx

        content = CONTENT

        def handler(request: httpx.Request) -> httpx.Response:
            range_hdr = request.headers.get("range", "")
            parts = range_hdr.split("=")[1].split("-")
            start, end = int(parts[0]), int(parts[1])
            return httpx.Response(206, content=content[start:end + 1])

        path = tmp_path / "out.fastq.gz"
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with _ProgressCtx() as p:
                task = p.add_task("test", total=len(content))
                await _download_segmented(
                    client, "https://example.com/out.fastq.gz",
                    path, len(content), "out.fastq.gz", p, task, n_segments=3,
                )

        parts_dir = tmp_path / ".out.fastq.gz.parts"
        assert not parts_dir.exists()


# ---------------------------------------------------------------------------
# Cross-file concurrency + aggregate error
# ---------------------------------------------------------------------------

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self, tmp_path: Path) -> None:
        """Verify that at most `concurrency` files download simultaneously."""
        peak: list[int] = []
        lock = asyncio.Lock()

        content = CONTENT
        sha, md5 = _checksums(content)
        n_files = 6
        concurrency = 2

        call_count = 0

        async def fake_download_one(*args, **kwargs):
            nonlocal call_count
            async with lock:
                call_count += 1
                peak.append(call_count)
            await asyncio.sleep(0.01)
            async with lock:
                call_count -= 1

        files = [
            DataFile(
                location=f"https://example.com/f{i}.fastq.gz",
                format="fastq",
                is_remote=True,
                sha256=sha,
                md5=md5,
                expected_filename=f"f{i}.fastq.gz",
            )
            for i in range(n_files)
        ]
        dataset = DataSet(name="test", files=files)

        with patch(
            "bioledger_isatab_schema.download._download_one",
            side_effect=fake_download_one,
        ):
            await download_remote_files(
                dataset, tmp_path, user_confirmed=True, concurrency=concurrency
            )

        assert max(peak) <= concurrency

    @pytest.mark.asyncio
    async def test_single_failure_raises_original_exception(self, tmp_path: Path) -> None:
        content = CONTENT
        sha, md5 = _checksums(content)

        files = [
            DataFile(
                location=f"https://example.com/f{i}.fastq.gz",
                format="fastq",
                is_remote=True,
                sha256=sha,
                md5=md5,
                expected_filename=f"f{i}.fastq.gz",
            )
            for i in range(2)
        ]
        dataset = DataSet(name="test", files=files)

        call_idx = 0

        async def fake_download_one(*args, **kwargs):
            nonlocal call_idx
            i = call_idx
            call_idx += 1
            if i == 0:
                raise ValueError("deliberate failure")

        with patch("bioledger_isatab_schema.download._download_one", side_effect=fake_download_one):
            with pytest.raises(ValueError, match="deliberate failure"):
                await download_remote_files(dataset, tmp_path, user_confirmed=True)

    @pytest.mark.asyncio
    async def test_multi_failure_raises_exception_group(self, tmp_path: Path) -> None:
        content = CONTENT
        sha, md5 = _checksums(content)

        files = [
            DataFile(
                location=f"https://example.com/f{i}.fastq.gz",
                format="fastq",
                is_remote=True,
                sha256=sha,
                md5=md5,
                expected_filename=f"f{i}.fastq.gz",
            )
            for i in range(3)
        ]
        dataset = DataSet(name="test", files=files)

        async def always_fail(*args, **kwargs):
            raise RuntimeError("boom")

        with patch("bioledger_isatab_schema.download._download_one", side_effect=always_fail):
            with pytest.raises(ExceptionGroup):
                await download_remote_files(dataset, tmp_path, user_confirmed=True)


# ---------------------------------------------------------------------------
# SRA-Toolkit opt-in path
# ---------------------------------------------------------------------------

class TestSRAPath:
    def test_extract_sra_accession_from_field(self) -> None:
        f = DataFile(
            location="https://example.com/SRR1234567_1.fastq.gz",
            is_remote=True,
            sha256="a" * 64,
            sra_accession="SRR1234567",
        )
        assert _extract_sra_accession(f) == "SRR1234567"

    def test_extract_sra_accession_from_url(self) -> None:
        f = DataFile(
            location="https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR713/SRR7133733_1.fastq.gz",
            is_remote=True,
            sha256="a" * 64,
        )
        assert _extract_sra_accession(f) == "SRR7133733"

    def test_extract_sra_returns_none_for_non_sra(self) -> None:
        f = DataFile(
            location="https://example.com/genome.fa.gz",
            is_remote=True,
            sha256="a" * 64,
        )
        assert _extract_sra_accession(f) is None

    @pytest.mark.asyncio
    async def test_sra_path_not_used_when_disabled(self, tmp_path: Path) -> None:
        """When use_sra_toolkit=False, _download_via_sra is never called."""
        content = CONTENT
        sha, md5 = _checksums(content)
        dataset = _make_dataset(content, sra_accession="SRR9999999")

        async def fake_download_one(*args, **kwargs):
            pass

        with patch(
            "bioledger_isatab_schema.download._download_via_sra"
        ) as mock_sra:
            with patch(
                "bioledger_isatab_schema.download._download_one",
                side_effect=fake_download_one,
            ):
                await download_remote_files(
                    dataset, tmp_path, user_confirmed=True,
                    use_sra_toolkit=False, n_segments=1,
                )
            mock_sra.assert_not_called()

    @pytest.mark.asyncio
    async def test_sra_path_skips_md5_verification(self, tmp_path: Path) -> None:
        """SRA path calls _verify_and_record with skip_md5=True."""
        content = CONTENT
        sha, _ = _checksums(content)
        dataset = _make_dataset(content, sra_accession="SRR1111111")
        # Give the file a wrong md5 — SRA path should not care
        dataset.files[0].md5 = "wrong_md5_value_here_x0000000"

        output_file = tmp_path / "file.fastq.gz"

        async def fake_sra(file, local_path, accession, expected_name, progress, download_dir):
            local_path.write_bytes(content)
            return sha, "irrelevant_md5", len(content)

        with patch("bioledger_isatab_schema.download._download_via_sra", side_effect=fake_sra):
            result = await download_remote_files(
                dataset, tmp_path, user_confirmed=True,
                use_sra_toolkit=True, n_segments=1,
            )

        assert result.files[0].downloaded_path == str(output_file)

    @pytest.mark.asyncio
    async def test_sra_path_raises_when_toolkit_missing(
        self, tmp_path: Path
    ) -> None:
        """If prefetch is missing and use_sra_toolkit=True, FileNotFoundError is raised.

        The SRA path is opt-in: if the user enables it but the toolkit is absent
        we surface the error rather than silently falling back to HTTPS.
        """
        content = CONTENT
        sha, md5 = _checksums(content)
        dataset = _make_dataset(content, sra_accession="SRR2222222")

        with patch("shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError, match="prefetch"):
                await download_remote_files(
                    dataset, tmp_path, user_confirmed=True,
                    use_sra_toolkit=True, n_segments=1,
                )


# ---------------------------------------------------------------------------
# aria2c opt-in path
# ---------------------------------------------------------------------------

class TestAria2cPath:
    @pytest.mark.asyncio
    async def test_aria2c_path_invoked_when_enabled_and_present(self, tmp_path: Path) -> None:
        content = CONTENT
        sha, md5 = _checksums(content)
        dataset = _make_dataset(content)

        aria2_calls: list[object] = []

        async def fake_aria2c(url, local_path, expected_name, progress):
            aria2_calls.append(url)
            local_path.write_bytes(content)
            return sha, md5, len(content)

        with patch("shutil.which", return_value="/usr/bin/aria2c"):
            with patch(
                "bioledger_isatab_schema.download._download_via_aria2c",
                side_effect=fake_aria2c,
            ):
                await download_remote_files(
                    dataset, tmp_path, user_confirmed=True, use_aria2c=True, n_segments=1
                )

        assert len(aria2_calls) == 1

    @pytest.mark.asyncio
    async def test_aria2c_falls_back_on_failure(self, tmp_path: Path) -> None:
        """When aria2c raises, _download_one falls back to HTTPS single-stream."""
        from bioledger_isatab_schema.download import _download_one, _ProgressCtx

        content = CONTENT
        sha, md5 = _checksums(content)
        file = _make_file(content)

        def handler(request: httpx.Request) -> httpx.Response:
            range_hdr = request.headers.get("range")
            if range_hdr:
                return httpx.Response(200, content=content,
                                      headers={"Content-Length": str(len(content))})
            return httpx.Response(200, content=content,
                                  headers={"Content-Length": str(len(content))})

        transport = httpx.MockTransport(handler)

        async def failing_aria2c(url, local_path, expected_name, progress):
            raise RuntimeError("aria2c failed")

        with patch("shutil.which", return_value="/usr/bin/aria2c"):
            with patch(
                "bioledger_isatab_schema.download._download_via_aria2c",
                side_effect=failing_aria2c,
            ):
                async with httpx.AsyncClient(transport=transport) as client:
                    with _ProgressCtx() as p:
                        await _download_one(
                            client, file, tmp_path, p,
                            n_segments=1, use_sra=False, use_aria2c=True,
                        )

        assert file.downloaded_path == str(tmp_path / "file.fastq.gz")


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_datafile_has_sra_accession_field(self) -> None:
        f = DataFile(location="x", sha256="a" * 64)
        assert f.sra_accession is None

    def test_manifest_file_has_sra_accession_field(self) -> None:
        from bioledger_isatab_schema.manifest import ManifestFile
        mf = ManifestFile(
            filename="f.fastq.gz",
            url="https://example.com/f.fastq.gz",
            format="fastq",
            md5="a" * 32,
        )
        assert mf.sra_accession is None

    def test_manifest_file_accepts_sra_accession(self) -> None:
        from bioledger_isatab_schema.manifest import ManifestFile
        mf = ManifestFile(
            filename="f.fastq.gz",
            url="https://example.com/f.fastq.gz",
            format="fastq",
            md5="a" * 32,
            sra_accession="SRR7133733",
        )
        assert mf.sra_accession == "SRR7133733"

    def test_manifest_to_datafiles_propagates_sra_accession(self) -> None:
        manifest = Manifest(
            study_type=StudyType.EXPERIMENTAL_DATA,
            insdc_accession="PRJNA450813",
            organism="Test",
            files=[
                ManifestFile(
                    filename="SRR1234567_1.fastq.gz",
                    url="https://example.com/SRR1234567_1.fastq.gz",
                    format="fastq",
                    md5="a" * 32,
                    sra_accession="SRR1234567",
                )
            ],
        )
        dfiles = manifest_to_datafiles(manifest)
        assert dfiles[0].sra_accession == "SRR1234567"

    @pytest.mark.asyncio
    async def test_download_remote_files_rejects_without_confirmation(
        self, tmp_path: Path
    ) -> None:
        dataset = DataSet(name="t", files=[])
        with pytest.raises(ValueError, match="confirm"):
            await download_remote_files(dataset, tmp_path, user_confirmed=False)

    @pytest.mark.asyncio
    async def test_download_remote_files_returns_unchanged_if_no_remote(
        self, tmp_path: Path
    ) -> None:
        dataset = DataSet(name="t", files=[DataFile(location="/local/file", sha256="a" * 64)])
        result = await download_remote_files(dataset, tmp_path, user_confirmed=True)
        assert result is dataset
