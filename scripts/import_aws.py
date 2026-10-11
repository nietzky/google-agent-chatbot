"""Scrape a bounded, resumable official AWS corpus and embed it into Elasticsearch.

Run from the project root: python scripts/import_aws.py
This imports overview + up to two linked troubleshooting/getting-started pages per
product, not entire manuals. Only public docs.aws.amazon.com pages are fetched.
"""

import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from chatbot.config import Settings
from chatbot.runtime import resources

ROOT = Path("knowledge/aws")
INDEX = "pingo-products-openai-v1"
EXTRA_PAGES = {
    "s3": ["https://docs.aws.amazon.com/AmazonS3/latest/userguide/troubleshoot-403-errors.html"],
    "lambda": ["https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html"],
}
AMBIGUOUS = {
    "batch",
    "transfer",
    "config",
    "health",
    "signer",
    "inspector",
    "detective",
    "organizations",
    "shield",
    "pcs",
    "drs",
}

# Stable product identifiers, selected from the official AWS documentation catalog.
PRODUCT_PATHS = """ec2 lambda lightsail batch elastic-beanstalk apprunner outposts
parallelcluster pcs ecr ecs eks autoscaling s3 ebs efs fsx aws-backup storagegateway
datasync transfer drs rds dynamodb documentdb elasticache keyspaces memorydb neptune
timestream vpc apigateway cloudfront route53 directconnect global-accelerator vpn
cloud-map verified-access vpc-lattice iam singlesignon acm privateca kms secretsmanager
cloudhsm signer cognito guardduty inspector macie detective network-firewall
firewall-manager shield waf security-lake verifiedpermissions audit-manager
directory-service cloudwatch cloudtrail cloudformation config systems-manager
organizations controltower health compute-optimizer servicecatalog appconfig
resilience-hub grafana prometheus athena emr glue lake-formation firehose kinesis
managed-flink msk opensearch-service redshift datazone eventbridge sns sqs
step-functions amazon-mq mwaa appflow appsync codebuild codedeploy codepipeline
codeartifact bedrock sagemaker""".split()


# Accept only HTTPS URLs on the official AWS documentation host and strip query strings and
# fragments. Raise on unsupported URLs before they enter the import workflow.
def canonical(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "docs.aws.amazon.com":
        raise ValueError("Only official AWS documentation is allowed")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


# Fetch an official documentation page, validate the resulting URL, and follow a bounded number
# of HTML refresh redirects. Return the final URL and parsed HTML or fail when the redirect
# limit is exceeded.
async def page(client, url):
    url = canonical(url)
    for _ in range(4):
        response = await client.get(url)
        response.raise_for_status()
        url = canonical(str(response.url))
        soup = BeautifulSoup(response.text, "html.parser")
        redirect = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
        if redirect and re.search(r"url=(.+)", redirect.get("content", ""), re.I):
            url = canonical(urljoin(url, re.search(r"url=(.+)", redirect["content"], re.I)[1]))
            continue
        return url, soup
    raise ValueError("Too many meta redirects")


# Select the main documentation body, remove navigation and executable/style elements, and
# extract readable text. Mutate the parsed document and reject missing or implausibly short
# content.
def article(soup):
    main = soup.select_one("#main-col-body") or soup.select_one("main")
    if main is None:
        raise ValueError("No documentation article body")
    for tag in main.select("script,style,nav,footer,.awsdocs-page-header-container"):
        tag.decompose()
    text = main.get_text("\n", strip=True)
    if len(text) < 200:
        raise ValueError("Article too short")
    return text


# Build the hundred-product catalog from official AWS documentation and ingest selected
# overview/troubleshooting pages with bounded concurrency. Save page snapshots and a resumable
# manifest, then verify indexed product coverage and fail if any products are missing.
async def main():
    assert len(PRODUCT_PATHS) == len(set(PRODUCT_PATHS)) == 100
    ROOT.mkdir(parents=True, exist_ok=True)
    settings = Settings(elasticsearch_index=INDEX)
    manifest_path = ROOT / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"index": INDEX, "documents": []}
    )
    imported = {d["source_id"]: d for d in manifest["documents"]}
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=45,
        headers={"User-Agent": "Pingo-local-documentation-importer/1.0"},
        limits=httpx.Limits(max_connections=4),
    ) as client:
        _, catalog = await page(client, "https://docs.aws.amazon.com/")
        products = []
        for slug in PRODUCT_PATHS:
            links = [
                a
                for a in catalog.select("a[href]")
                if urlsplit(urljoin("https://docs.aws.amazon.com/", a["href"])).path.rstrip("/")
                == "/" + slug
            ]
            if not links:
                raise ValueError(f"Missing official catalog entry: {slug}")
            name = re.sub(r"\s+", " ", links[0].get_text(" ", strip=True))
            products.append(
                {
                    "id": slug,
                    "name": name,
                    "docs_url": f"https://docs.aws.amazon.com/{slug}/",
                    "aliases": [] if slug in AMBIGUOUS else [slug.replace("-", " ")],
                }
            )
        (ROOT / "products.json").write_text(json.dumps(products, indent=2), encoding="utf-8")
        async with resources(settings) as (_, knowledge):
            await knowledge.ensure_index()
            semaphore = asyncio.Semaphore(3)

            # Skip a product whose saved coverage and indexed chunk count already match unless
            # refresh is requested. Otherwise discover selected guide/troubleshooting pages,
            # ingest bounded text, and update snapshots and manifest; report individual product
            # failures so other imports can continue.
            async def ingest_product(product):
                async with semaphore:
                    slug = product["id"]
                    try:
                        existing = [d for d in imported.values() if d["product_id"] == slug]
                        if (
                            "--refresh" not in sys.argv
                            and existing
                            and any(d.get("coverage_version") == 3 for d in existing)
                        ):
                            count = await knowledge.es.count(
                                index=INDEX, query={"term": {"product_id": slug}}
                            )
                            if count["count"] == sum(d["chunks"] for d in existing):
                                print(f"Cached {slug}: {count['count']} chunks", flush=True)
                                return
                        url, soup = await page(client, product["docs_url"])
                        if soup.select_one("#main-col-body") is None:
                            guides = [
                                a
                                for a in soup.select("a[href]")
                                if re.search(
                                    r"(user|developer|administration|administrator) guide",
                                    a.get_text(" ", strip=True),
                                    re.I,
                                )
                            ]
                            if not guides:
                                guides = [
                                    a
                                    for a in soup.select("a[href]")
                                    if re.search(
                                        r"/latest/(dg|userguide|devguide|UserGuide)(/|$)", a["href"]
                                    )
                                ]
                            if not guides:
                                raise ValueError("No guide link")
                            url, soup = await page(
                                client,
                                urljoin(
                                    url,
                                    guides[0]["href"]
                                    if ".html" in guides[0]["href"]
                                    else guides[0]["href"].rstrip("/") + "/",
                                ),
                            )
                        candidates = []
                        try:
                            toc_response = await client.get(urljoin(url, "toc-contents.json"))
                            toc_response.raise_for_status()

                            ranked = []

                            # Recursively inspect table-of-contents entries and collect
                            # troubleshooting candidates with depth and title-length ranking
                            # data into the enclosing list.
                            def walk(items, depth=0):
                                for item in items:
                                    if re.search(
                                        r"troubleshoot|common errors", item.get("title", ""), re.I
                                    ) and item.get("href"):
                                        ranked.append(
                                            (
                                                depth,
                                                len(item["title"]),
                                                canonical(urljoin(url, item["href"])),
                                            )
                                        )
                                    walk(item.get("contents", []), depth + 1)

                            walk(toc_response.json().get("contents", []))
                            candidates.extend(target for _, _, target in sorted(ranked))
                        except (httpx.HTTPError, ValueError):
                            pass
                        for a in soup.select("a[href]"):
                            label = a.get_text(" ", strip=True)
                            if re.search(
                                r"troubleshoot|getting started|common errors", label, re.I
                            ):
                                try:
                                    target = canonical(urljoin(url, a["href"]))
                                except ValueError:
                                    continue
                                if (
                                    target != url
                                    and target.endswith(".html")
                                    and target not in candidates
                                ):
                                    candidates.append(target)
                        pages = [(url, soup)]
                        for target in dict.fromkeys(candidates[:2] + EXTRA_PAGES.get(slug, [])):
                            try:
                                pages.append(await page(client, target))
                            except (httpx.HTTPError, ValueError):
                                pass
                        for page_url, page_soup in pages:
                            try:
                                full_text = article(page_soup)
                            except ValueError:
                                if page_url == url:
                                    raise
                                continue
                            text = product["name"] + "\n" + full_text[:60000]
                            source = (
                                f"aws/{slug}/{hashlib.sha256(page_url.encode()).hexdigest()[:12]}"
                            )
                            title = (
                                page_soup.title.get_text(strip=True)
                                if page_soup.title
                                else product["name"]
                            )
                            metadata = {
                                "product_id": slug,
                                "product_name": product["name"],
                                "url": page_url,
                                "title": title,
                            }
                            count = await knowledge.ingest(source, text, metadata)
                            (ROOT / f"{slug}-{source.rsplit('/', 1)[1]}.txt").write_text(
                                text, encoding="utf-8"
                            )
                            imported[source] = {
                                "source_id": source,
                                "coverage_version": 3,
                                **metadata,
                                "chunks": count,
                                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                "truncated": len(full_text) > 60000,
                            }
                            manifest["documents"] = list(imported.values())
                            manifest_path.write_text(
                                json.dumps(manifest, indent=2), encoding="utf-8"
                            )
                        print(f"Imported {slug}: {len(pages)} pages", flush=True)
                    except Exception as exc:
                        print(f"FAILED {slug}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)

            await asyncio.gather(*(ingest_product(p) for p in products))
            result = await knowledge.es.search(
                index=INDEX,
                size=0,
                aggs={"products": {"terms": {"field": "product_id", "size": 101}}},
            )
            covered = {b["key"] for b in result["aggregations"]["products"]["buckets"]}
            missing = set(PRODUCT_PATHS) - covered
            print(f"Verified {len(covered)}/100 products. Missing: {sorted(missing)}", flush=True)
            if missing:
                raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
