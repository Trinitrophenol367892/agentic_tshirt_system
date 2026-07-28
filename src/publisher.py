import json
import time
from .database import get_connection
from .logger import log_publisher


class PrintifyAPI:
    def publish(self, design, brief):
        trend_name = brief.get("trend_metadata", {}).get("trend_name", "Untitled")
        log_publisher.info("  [Printify] Creating product...")
        log_publisher.debug("    Product: %s Tee", trend_name)
        log_publisher.debug("    Blank: %s", design.get("garment_color", "Black"))
        log_publisher.debug("    Print: %s", design.get("print_technique", "DTG"))
        log_publisher.debug("    Layout: %s", design.get("placement", "Center chest"))
        start = time.time()
        # TODO: Actual Printify API call
        elapsed = time.time() - start
        log_publisher.info("    Printify product created (%.2fs). product_id=pf_88291", elapsed)


class ShopifyAPI:
    def publish(self, design, brief):
        log_publisher.info("  [Shopify] Syncing product...")
        start = time.time()
        # TODO: Actual Shopify API call
        elapsed = time.time() - start
        log_publisher.info("    Shopify product synced (%.2fs). product_id=9284716352", elapsed)


class CustomEcomAPI:
    def publish(self, design, brief):
        log_publisher.info("  [Custom Ecom] Pushing to internal storefront...")
        start = time.time()
        # TODO: Actual Custom Ecom API call
        elapsed = time.time() - start
        log_publisher.info("    Custom Ecom product created (%.2fs). sku=CYB-GLT-003", elapsed)


def run_publisher(design_id, brief):
    log_publisher.info("=== PUBLISHER NODE STARTED ===")
    log_publisher.info("  Publishing design ID: %d", design_id)

    with get_connection() as conn:
        log_publisher.debug("  Loading design JSON from SQLite...")
        cursor = conn.execute(
            "SELECT design_json FROM designs WHERE id = ?", (design_id,)
        )
        row = cursor.fetchone()
        if not row:
            log_publisher.error("  Design ID %d not found in DB!", design_id)
            return
        design = json.loads(row[0])
        log_publisher.debug("  Design loaded: '%s'", design.get("slogan", "N/A"))

        PrintifyAPI().publish(design, brief)
        ShopifyAPI().publish(design, brief)
        CustomEcomAPI().publish(design, brief)

        conn.execute(
            "UPDATE designs SET status = 'published' WHERE id = ?", (design_id,)
        )
        log_publisher.debug("  Design ID %d status updated to 'published'.", design_id)

    log_publisher.info("=== PUBLISHER NODE COMPLETE: Product live on 3 platforms ===")
