import unittest
from logistics_routing import RoutingPolicy, best_insertion, initial_route_plan

POLICY = RoutingPolicy(2.0, 5.0, 15.0, 10.0)

def stop(stop_id, order_id, typ, lat, lng, delta):
    return {"stop_id":stop_id,"order_id":order_id,"stop_type":typ,
            "latitude":lat,"longitude":lng,"address":typ,"quantity_delta":delta}

class RoutingTests(unittest.TestCase):
    def test_initial_route(self):
        order={"order_id":1,"quantity":60,"pickup_latitude":23.20,"pickup_longitude":77.40,
               "delivery_latitude":23.25,"delivery_longitude":77.45,
               "pickup_address":"P1","delivery_address":"D1"}
        plan=initial_route_plan(start_latitude=23.18,start_longitude=77.38,
            order=order,vehicle_capacity_kg=100)
        self.assertTrue(plan["compatible"])
        self.assertLessEqual(plan["peak_load_kg"],100)

    def test_on_route_pickup_small_delivery_extension(self):
        existing=[stop(10,1,"PICKUP",23.20,77.40,60),stop(11,1,"DELIVERY",23.30,77.50,-60)]
        order={"order_id":2,"quantity":30,"pickup_latitude":23.25,"pickup_longitude":77.45,
               "delivery_latitude":23.32,"delivery_longitude":77.52,
               "pickup_address":"P2","delivery_address":"D2"}
        plan=best_insertion(start_latitude=23.18,start_longitude=77.38,existing_stops=existing,
            order=order,vehicle_capacity_kg=100,policy=POLICY)
        self.assertTrue(plan["compatible"])
        self.assertLessEqual(plan["pickup_deviation_km"],2.0)
        self.assertLessEqual(plan["peak_load_kg"],100)

    def test_far_pickup_rejected(self):
        existing=[stop(10,1,"PICKUP",23.20,77.40,40),stop(11,1,"DELIVERY",23.30,77.50,-40)]
        order={"order_id":2,"quantity":20,"pickup_latitude":23.50,"pickup_longitude":77.75,
               "delivery_latitude":23.52,"delivery_longitude":77.77,
               "pickup_address":"P2","delivery_address":"D2"}
        plan=best_insertion(start_latitude=23.18,start_longitude=77.38,existing_stops=existing,
            order=order,vehicle_capacity_kg=100,policy=POLICY)
        self.assertFalse(plan["compatible"])
        self.assertEqual(plan["reason"],"PICKUP_OUTSIDE_ROUTE_CORRIDOR")

    def test_capacity_is_segment_based(self):
        existing=[stop(10,1,"PICKUP",23.20,77.40,80),stop(11,1,"DELIVERY",23.24,77.44,-80)]
        order={"order_id":2,"quantity":50,"pickup_latitude":23.24,"pickup_longitude":77.44,
               "delivery_latitude":23.27,"delivery_longitude":77.47,
               "pickup_address":"P2","delivery_address":"D2"}
        plan=best_insertion(start_latitude=23.18,start_longitude=77.38,existing_stops=existing,
            order=order,vehicle_capacity_kg=100,policy=POLICY)
        self.assertTrue(plan["compatible"])
        self.assertLessEqual(plan["peak_load_kg"],100)

if __name__=="__main__":
    unittest.main()
