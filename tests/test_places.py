import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from places import customer_map_url, extract_place_markup, extract_places, place_pages


def page(url, title, markup="", text=""):
    return {"url":url,"title":title,"text":text, "place_data":extract_place_markup(markup,url)}


class PlacesTest(unittest.TestCase):
    def test_dining_display_name_and_building_address(self):
        url = "https://union.school.edu/"
        rows = extract_places([page(url, "Student Union", """
            <h2>Campus Union Building Hours</h2><h3>Blue Bistro Hours</h3>
            <h3>Limited Hours</h3><h2>Partners</h2>
            <a href="https://dining.school.edu/locations/old-brand">Blue Bistro</a>
            <a href="https://dining.school.edu/locations/invented">Unconfirmed</a>
            <a href="https://www.google.com/maps?q=different">100 Market Street, College Town</a>
        """)], "School", "school.edu")
        self.assertEqual(["Blue Bistro"], [p["name"] for p in rows])
        self.assertIn("Campus Union", rows[0]["address"])
        self.assertIn("100 Market Street", rows[0]["address"])
        self.assertEqual(url, rows[0]["source_url"])
        self.assertNotIn("different", rows[0]["maps_url"])

    def test_schema_branches_sources_and_keyless_links(self):
        url = "https://example.edu/stores"
        markup = """<script type="application/ld+json">{"@graph":[{"@type":"BookStore",
           "name":"Oak Books","address":{"streetAddress":"10 West Street","addressLocality":"Oakton"}},
           {"@type":"BookStore","name":"Oak Books","address":{"streetAddress":"20 East Street"}},
           {"@type":"Person","name":"Person without venue evidence"}]}</script>"""
        rows = extract_places([page(url,"Our stores",markup), page("https://example.edu.bad.test","Fake",markup)], "Example", "www.example.edu")
        self.assertEqual(2,len(rows))
        self.assertTrue(all(p["category"]=="bookstore" and p["source_url"]==url for p in rows))
        for r in rows:
            search = parse_qs(urlsplit(r["maps_url"]).query)
            directions = parse_qs(urlsplit(r["directions_url"]).query)
            self.assertEqual(search["query"], directions["destination"])
            self.assertEqual(["1"],search["api"])
            self.assertIn(r["address"],search["query"][0])
        self.assertEqual(1,len(place_pages(rows)))
        self.assertIn("20 East Street",place_pages(rows)[0]["text"])

    def test_pdf_shop_inside_parent_and_sourced_street_address(self):
        store = page("https://school.edu/guide.pdf", "School Family Handbook", text=
                     "School Student Stores\nDANIELS BUILDING | Website\nWe sell course materials.\n"
                     "BULL’S HEAD BOOKSHOP\nBull’s Head Bookshop is located on the third floor of Student Stores.")
        pharmacy = page("https://school.edu/pharmacy", "Pharmacy", """
           <h2>Student Stores Pharmacy</h2>
           <address>Address: Daniels Student Stores, 207 South Rd. College Town</address>
           <h2>Central Pharmacy</h2><address>200 University Road</address>""")
        rows = extract_places([store,pharmacy],"School","school.edu")
        self.assertEqual({"Daniels Student Stores","Bull’s Head Bookshop"}, {p["name"] for p in rows})
        bookshop = next(p for p in rows if p["name"]=="Bull’s Head Bookshop")
        self.assertEqual("on the third floor of Student Stores",bookshop["address"])
        self.assertEqual("https://school.edu/guide.pdf",bookshop["source_url"])

    def test_directory_with_heading_and_address(self):
        url="https://example.edu/dining"
        rows=extract_places([page(url,"Dining Locations","<h1>Dining Locations</h1><h2>The Maple Room</h2><address>32 College Ave, Oakton</address>")], "Example", "example.edu")
        self.assertEqual([("The Maple Room","dining","32 College Ave, Oakton")],[(r["name"],r["category"],r["address"]) for r in rows])

    def test_generic_navigation_or_third_party_content_cannot_create_venues(self):
        rows = extract_places([page("https://school.edu/", "Home",
                 '<h3>Dining locations</h3><a href="https://directory.example/locations/pizza">Pizza Palace</a><a href="http://[bad">Bad link</a><template><script type="application/ld+json">{"@type":"Restaurant","name":"Hidden Placeholder"}</script></template>',
                 text="You can find a restaurant or a bookstore nearby.")],"School","school.edu")
        self.assertEqual([],rows)

    def test_customer_map_and_venue_queries_use_institution_not_first_department(self):
        department = page("https://business.unc.edu/", "Business School",
            '<script type="application/ld+json">[{"@type":"CollegeOrUniversity","name":"UNC Kenan-Flagler Business School","address":"300 Kenan Center Drive"},'
            '{"@type":"CollegeOrUniversity","name":"University of North Carolina at Chapel Hill"},'
            '{"@type":"Restaurant","name":"Campus Bistro"}]</script>')
        home = page("https://www.unc.edu/", "The University of North Carolina at Chapel Hill")
        for pages in ([department], [department, home], [home, department]):
            records = extract_places(pages, "The University of North Carolina", "unc.edu")
            destination = parse_qs(urlsplit(customer_map_url(records, "The University of North Carolina","unc.edu")).query)["query"][0]
            self.assertIn("University of North Carolina at Chapel Hill", destination)
            self.assertNotIn("Kenan",destination)
            venue = next(r for r in records if r["name"] == "Campus Bistro")
            self.assertIn("Chapel Hill", parse_qs(urlsplit(venue["directions_url"]).query)["destination"][0])
            self.assertNotIn("Kenan",venue["maps_url"])

    def test_business_map_has_safe_search_fallback_and_avoids_arbitrary_branch(self):
        customer = "Willow Books"
        links = [page("https://willow.example/locations", "Our shops",
            '<script type="application/ld+json">[{"@type":"BookStore","name":"Willow Books","address":"11 South Road"},'
            '{"@type":"BookStore","name":"Willow Books","address":"24 North Road"}]</script>')]
        rows = extract_places(links, customer, "willow.example")
        for entries in ([], rows):
            query = parse_qs(urlsplit(customer_map_url(entries,customer,"willow.example")).query)
            self.assertEqual([customer], query["query"])

    def test_dining_detail_excludes_menu_items_and_controls(self):
        url="https://dining.example.edu/locations/zayka/"
        markup = '<h1>Zayka Indian Grill</h1><h2>Zayka Dairy-Free Chicken Curry</h2>' + "".join(
            f'<a href="{target}">{label}</a>' for target, label in [
                ("#main","Skip to Main Content"), ("","News"), (url, "OneCard Login"),
                (url+"#item", "Zayka Dairy-Free Chicken Curry"),
                ("/locations/another/","Another Café"), (url, "Mobile Ordering")])
        records = extract_places([page(url,"Zayka Indian Grill - Campus Dining",markup)], "Example","example.edu")
        self.assertEqual(["Zayka Indian Grill"],[r["name"] for r in records])

    def test_office_inside_store_building_is_not_a_second_store(self):
        url="https://care.example.edu/about/locations"
        markup="<h2>Campus Care</h2><address>Student Stores Building 2nd Floor, 207 South Road, College Town</address>"
        self.assertEqual([],extract_places([page(url,"Campus care",markup)],"Example","example.edu"))
